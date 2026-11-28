"""Seat holds: a buyer keeps seats for a while, so no one else can buy them."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import Engine, text

from app.seed import user_key

NewEvent = Callable[..., tuple[int, list[int]]]


def hold(client: httpx.Client, key: str, event_id: int, seat_ids: list[int]) -> httpx.Response:
    return client.put(
        f"/events/{event_id}/hold", headers={"X-API-Key": key}, json={"seat_ids": seat_ids}
    )


def buy(client: httpx.Client, key: str, seat_ids: list[int]) -> httpx.Response:
    return client.post("/orders", headers={"X-API-Key": key}, json={"seat_ids": seat_ids})


def seat_ids(client: httpx.Client, event_id: int, status: str) -> list[int]:
    page = client.get(f"/events/{event_id}/seats", params={"status": status, "limit": 100})
    assert page.status_code == 200, page.text
    return [s["id"] for s in page.json()["items"]]


def available(client: httpx.Client, event_id: int) -> int:
    return client.get(f"/events/{event_id}").json()["available_seats"]


def test_a_hold_takes_seats_off_sale_for_everyone_else(
    client: httpx.Client, users, new_event: NewEvent
) -> None:
    event_id, seats = new_event(seats=4)
    me, rival = users.key(), users.key()

    held = hold(client, me, event_id, seats[:2])
    assert held.status_code == 200, held.text
    body = held.json()
    assert body["event_id"] == event_id and body["seat_ids"] == seats[:2]
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(minutes=9) < expires - datetime.now(UTC) <= timedelta(minutes=10)

    # Everyone sees the seats as held, and the count drops.
    assert seat_ids(client, event_id, "available") == seats[2:]
    assert seat_ids(client, event_id, "held") == seats[:2]
    assert available(client, event_id) == 2
    listed = client.get("/events", params={"limit": 100}).json()["items"]
    assert next(e for e in listed if e["id"] == event_id)["available_seats"] == 2

    # A rival can neither buy nor hold them.
    taken = buy(client, rival, [seats[0]])
    assert taken.status_code == 409
    assert taken.json() == {"detail": "One or more seats are held by another buyer"}
    assert hold(client, rival, event_id, [seats[1]]).status_code == 409

    # The holder can buy them, and then the hold is gone.
    assert buy(client, me, seats[:2]).status_code == 201
    assert client.get(f"/events/{event_id}/hold", headers={"X-API-Key": me}).status_code == 404
    assert seat_ids(client, event_id, "sold") == seats[:2]


def test_holding_again_replaces_the_hold_and_release_frees_the_seats(
    client: httpx.Client, user: str, new_event: NewEvent
) -> None:
    event_id, seats = new_event(seats=3)
    assert hold(client, user, event_id, [seats[0]]).status_code == 200
    assert hold(client, user, event_id, [seats[1], seats[2]]).status_code == 200

    mine = client.get(f"/events/{event_id}/hold", headers={"X-API-Key": user})
    assert mine.status_code == 200 and mine.json()["seat_ids"] == [seats[1], seats[2]]
    assert seat_ids(client, event_id, "available") == [seats[0]]

    for _ in range(2):  # releasing twice is fine
        released = client.delete(f"/events/{event_id}/hold", headers={"X-API-Key": user})
        assert released.status_code == 204
    assert seat_ids(client, event_id, "available") == seats
    assert client.get(f"/events/{event_id}/hold", headers={"X-API-Key": user}).status_code == 404


def test_an_expired_hold_frees_the_seats(
    client: httpx.Client, engine: Engine, users, new_event: NewEvent
) -> None:
    event_id, seats = new_event(seats=2)
    me, rival = users.key(), users.key()
    assert hold(client, me, event_id, seats).status_code == 200

    # Wind the clock forward for this hold only.
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE seat_holds SET expires_at = now() - interval '1 second' WHERE event_id = :e"
            ),
            {"e": event_id},
        )

    assert available(client, event_id) == 2
    assert client.get(f"/events/{event_id}/hold", headers={"X-API-Key": me}).status_code == 404
    assert hold(client, rival, event_id, [seats[0]]).status_code == 200
    assert buy(client, me, [seats[1]]).status_code == 201


def test_bad_holds_are_refused(client: httpx.Client, users, new_event: NewEvent) -> None:
    event_id, seats = new_event(seats=12)
    _, other_event_seats = new_event(seats=1)
    me = users.key()

    assert hold(client, me, event_id, other_event_seats).status_code == 404
    assert hold(client, me, 999_999, seats[:1]).status_code == 404
    assert hold(client, me, event_id, [seats[0], seats[0]]).status_code in (409, 422)
    assert hold(client, me, event_id, seats[:11]).status_code == 422
    anonymous = client.put(f"/events/{event_id}/hold", json={"seat_ids": seats[:1]})
    assert anonymous.status_code == 401

    # Sold seats can't be held.
    assert buy(client, me, seats[:8]).status_code == 201
    assert hold(client, users.key(), event_id, [seats[0]]).status_code == 409
    # The per-buyer limit counts what you own: 8 bought, so 3 more is too many.
    over = hold(client, me, event_id, seats[8:11])
    assert over.status_code == 409
    assert over.json()["detail"] == "You can hold at most 10 seats for one event"
    assert hold(client, me, event_id, seats[8:10]).status_code == 200


@pytest.mark.catches_hold_race
def test_twenty_buyers_hold_one_seat_at_once_exactly_one_gets_it(
    base_url: str, client: httpx.Client, new_event: NewEvent
) -> None:
    # Five rounds: the broken check (hold_race) let two or more holders win in
    # about two rounds out of three, so one round alone could miss it.
    keys = [user_key(n) for n in range(100, 120)]
    with httpx.Client(base_url=base_url, timeout=60) as shared:
        for _ in range(5):
            event_id, seats = new_event(seats=1)
            start = threading.Barrier(len(keys))

            def attempt(key: str, event_id: int = event_id, start=start, seats=seats) -> int:
                start.wait()
                return hold(shared, key, event_id, seats).status_code

            with ThreadPoolExecutor(max_workers=len(keys)) as pool:
                statuses = list(pool.map(attempt, keys))

            assert statuses.count(200) == 1, statuses
            assert statuses.count(409) == len(keys) - 1
            assert seat_ids(client, event_id, "held") == seats


def race_hold_and_order(base_url: str, event_id: int, seats: list[int]) -> tuple[int, int]:
    """One buyer holds and another buys the same seat at the same instant."""
    start = threading.Barrier(2)
    with httpx.Client(base_url=base_url, timeout=60) as shared:

        def go(action: str) -> int:
            start.wait()
            if action == "hold":
                return hold(shared, user_key(120), event_id, seats).status_code
            return buy(shared, user_key(121), seats).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            held, bought = pool.map(go, ["hold", "buy"])
    return held, bought


def test_a_hold_and_an_order_racing_for_one_seat_never_both_win(
    base_url: str, client: httpx.Client, new_event: NewEvent
) -> None:
    for _ in range(10):
        event_id, seats = new_event(seats=1)
        held, bought = race_hold_and_order(base_url, event_id, seats)
        assert sorted([held, bought]) in ([200, 409], [201, 409]), (held, bought)
        if bought == 201:
            # The buyer won, so no hold may survive on the sold seat.
            assert seat_ids(client, event_id, "held") == []

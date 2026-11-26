"""Functional tests for placing and reading orders."""

from __future__ import annotations

import httpx


def test_buying_seats_creates_an_order(client: httpx.Client, new_event, user) -> None:
    event_id, seats = new_event(seats=3, price_cents=2000)
    headers = {"X-API-Key": user}

    created = client.post("/orders", headers=headers, json={"seat_ids": seats[:2]})
    assert created.status_code == 201
    order = created.json()
    assert order["total_cents"] == 4000
    assert sorted(i["seat_id"] for i in order["items"]) == sorted(seats[:2])

    fetched = client.get(f"/orders/{order['id']}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json() == order

    listed = client.get("/orders", headers=headers).json()
    assert [o["id"] for o in listed["items"]] == [order["id"]]

    assert client.get(f"/events/{event_id}").json()["available_seats"] == 1


def test_a_sold_seat_cannot_be_bought_again(client: httpx.Client, new_event, users) -> None:
    _, seats = new_event(seats=1)
    first = client.post("/orders", headers={"X-API-Key": users.key()}, json={"seat_ids": seats})
    second = client.post("/orders", headers={"X-API-Key": users.key()}, json={"seat_ids": seats})
    assert first.status_code == 201
    assert second.status_code == 409


def test_one_taken_seat_fails_the_whole_order(client: httpx.Client, new_event, users) -> None:
    event_id, seats = new_event(seats=3)
    client.post("/orders", headers={"X-API-Key": users.key()}, json={"seat_ids": seats[:1]})

    response = client.post("/orders", headers={"X-API-Key": users.key()}, json={"seat_ids": seats})
    assert response.status_code == 409
    # Nothing from the failed order was kept.
    assert client.get(f"/events/{event_id}").json()["available_seats"] == 2


def test_unknown_seat_is_404(client: httpx.Client, user) -> None:
    response = client.post(
        "/orders", headers={"X-API-Key": user}, json={"seat_ids": [9_000_000_000]}
    )
    assert response.status_code == 404


def test_duplicate_seat_in_one_order_is_refused(client: httpx.Client, new_event, user) -> None:
    _, seats = new_event(seats=1)
    response = client.post(
        "/orders", headers={"X-API-Key": user}, json={"seat_ids": [seats[0], seats[0]]}
    )
    assert response.status_code == 409


def test_order_size_is_validated(client: httpx.Client, user) -> None:
    headers = {"X-API-Key": user}
    assert client.post("/orders", headers=headers, json={"seat_ids": []}).status_code == 422
    too_many = {"seat_ids": list(range(1, 12))}
    assert client.post("/orders", headers=headers, json=too_many).status_code == 422
    assert client.post("/orders", headers=headers, json={"seat_ids": [0]}).status_code == 422


def test_orders_list_is_newest_first(client: httpx.Client, new_event, user) -> None:
    _, seats = new_event(seats=2)
    headers = {"X-API-Key": user}
    ids = [
        client.post("/orders", headers=headers, json={"seat_ids": [s]}).json()["id"] for s in seats
    ]
    listed = client.get("/orders", headers=headers).json()["items"]
    assert [o["id"] for o in listed] == list(reversed(ids))


def test_order_items_carry_seat_labels(client: httpx.Client, new_event, user) -> None:
    _, seats = new_event(seats=3)
    headers = {"X-API-Key": user}
    order = client.post("/orders", headers=headers, json={"seat_ids": [seats[2], seats[0]]}).json()
    expected = [{"seat_id": seats[0], "label": "S-1"}, {"seat_id": seats[2], "label": "S-3"}]

    def labels(o: dict) -> list[dict]:
        return [{"seat_id": i["seat_id"], "label": i["label"]} for i in o["items"]]

    assert labels(order) == expected
    assert labels(client.get(f"/orders/{order['id']}", headers=headers).json()) == expected
    assert labels(client.get("/orders", headers=headers).json()["items"][0]) == expected


def test_reading_orders_costs_the_same_queries_however_many_items(
    client: httpx.Client, new_event, users
) -> None:
    # The labels come from a join, not a query per item (N+1).
    def queries(response: httpx.Response) -> int:
        assert response.status_code == 200
        return int(response.headers["X-DB-Queries"])

    small_user, big_user = users.key(), users.key()
    _, seats = new_event(seats=11)
    small = client.post("/orders", headers={"X-API-Key": small_user}, json={"seat_ids": seats[:1]}).json()
    big = [
        client.post("/orders", headers={"X-API-Key": big_user}, json={"seat_ids": pair}).json()
        for pair in (seats[1:3], seats[3:5], seats[5:7], seats[7:9], seats[9:11])
    ]

    assert queries(client.get("/orders", headers={"X-API-Key": big_user})) == queries(
        client.get("/orders", headers={"X-API-Key": small_user})
    )
    assert queries(client.get(f"/orders/{big[0]['id']}", headers={"X-API-Key": big_user})) == queries(
        client.get(f"/orders/{small['id']}", headers={"X-API-Key": small_user})
    )

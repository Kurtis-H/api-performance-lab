"""50 buyers, 1 seat: exactly one of them gets it."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.seed import user_key

BUYERS = 50


@pytest.mark.catches_race
def test_fifty_buyers_one_seat_exactly_one_wins(base_url: str, client, new_event) -> None:
    event_id, seats = new_event(seats=1)
    keys = [user_key(n) for n in range(100, 100 + BUYERS)]
    start = threading.Barrier(BUYERS)

    with httpx.Client(
        base_url=base_url,
        timeout=60,
        limits=httpx.Limits(max_connections=BUYERS, max_keepalive_connections=BUYERS),
    ) as shared:

        def buy(key: str) -> int:
            start.wait()  # release everyone at once
            response = shared.post("/orders", headers={"X-API-Key": key}, json={"seat_ids": seats})
            return response.status_code

        with ThreadPoolExecutor(max_workers=BUYERS) as pool:
            statuses = list(pool.map(buy, keys))

    assert statuses.count(201) == 1, f"winners: {statuses.count(201)}, statuses: {statuses}"
    assert statuses.count(409) == BUYERS - 1

    # The database agrees: one seat sold, one order line for it.
    stats = client.get(f"/events/{event_id}/stats").json()
    assert stats["seats_sold"] == 1

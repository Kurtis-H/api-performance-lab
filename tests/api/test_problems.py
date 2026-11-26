"""One test per performance problem.

Each test passes with the fix in place and fails when its problem is switched
on with PERF_BUGS. scripts/prove_problems.py checks both directions, so a test
that can't actually catch its problem gets noticed.

The race problem's test is in test_concurrency.py.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import Engine, text

from app.db import SEAT_LOOKUP_INDEX
from tests.api.conftest import metric

QUERY_BUDGET_EVENT_LIST = 3


@pytest.mark.catches_n_plus_one
def test_event_list_query_count_does_not_grow_with_page_size(client: httpx.Client) -> None:
    small = client.get("/events", params={"limit": 2})
    large = client.get("/events", params={"limit": 20})
    small_queries = int(small.headers["X-DB-Queries"])
    large_queries = int(large.headers["X-DB-Queries"])

    assert large_queries <= QUERY_BUDGET_EVENT_LIST, (
        f"GET /events?limit=20 ran {large_queries} queries; budget is {QUERY_BUDGET_EVENT_LIST}"
    )
    assert large_queries == small_queries


@pytest.mark.catches_missing_index
def test_seats_have_an_index_for_event_and_status_lookups(engine: Engine) -> None:
    # Checked structurally rather than with EXPLAIN: on a small test table the
    # planner may skip a perfectly good index, which would make this flaky.
    # The speed difference is measured by the k6 runs in docs/problems.
    with engine.connect() as conn:
        definitions = [
            row[0]
            for row in conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE tablename = 'seats'")
            )
        ]
    leading = [d for d in definitions if "(event_id, status" in d]
    assert leading, (
        "No index on seats starts with (event_id, status); every seat count and "
        f"lookup scans the whole table. Indexes found: {definitions}"
    )
    assert any(SEAT_LOOKUP_INDEX in d for d in leading)


@pytest.mark.catches_memory_leak
def test_debug_buffer_stays_bounded(client: httpx.Client, settings) -> None:
    for _ in range(settings.debug_buffer_size + 50):
        assert client.get("/events/1/seats", params={"limit": 5}).status_code == 200
    held = metric(client, "debug_buffer_entries")
    assert held <= settings.debug_buffer_size, (
        f"Debug buffer holds {held:.0f} entries after "
        f"{settings.debug_buffer_size + 50} requests; cap is {settings.debug_buffer_size}"
    )


@pytest.mark.catches_no_page_limit
def test_list_endpoints_cap_page_size(client: httpx.Client) -> None:
    assert client.get("/events", params={"limit": 100_000}).status_code == 422
    assert client.get("/events/1/seats", params={"limit": 100_000}).status_code == 422

    default_page = client.get("/events").json()
    assert default_page["limit"] == 20
    assert len(default_page["items"]) <= 20


@pytest.mark.catches_cache_stampede
def test_expired_stats_are_recomputed_once_under_load(
    base_url: str, client: httpx.Client, new_event
) -> None:
    event_id, _ = new_event(seats=2)  # a fresh event, so its stats are not cached yet
    callers = 20
    start = threading.Barrier(callers)
    before = metric(client, "stats_recomputes_total")

    with httpx.Client(
        base_url=base_url, timeout=60, limits=httpx.Limits(max_connections=callers)
    ) as shared:

        def fetch(_: int) -> int:
            start.wait()
            return shared.get(f"/events/{event_id}/stats").status_code

        with ThreadPoolExecutor(max_workers=callers) as pool:
            statuses = list(pool.map(fetch, range(callers)))

    assert statuses == [200] * callers
    recomputes = metric(client, "stats_recomputes_total") - before
    assert recomputes == 1, f"{callers} concurrent requests caused {recomputes:.0f} recomputes"

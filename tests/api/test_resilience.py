"""Database failures: time limits, 503s and readiness (resilience-lab RL-01 to RL-03).

resilience-lab injects these faults into the deployed stack with Toxiproxy.
These tests pin the same behavior here, where it's built.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import httpx
import pytest
import uvicorn
from sqlalchemy import Engine, text

from app.config import Settings
from app.main import create_app
from tests.api.conftest import metric


@contextmanager
def serve(settings: Settings, lifespan: Literal["on", "off"] = "on") -> Iterator[httpx.Client]:
    """A second server, with its own settings, for one test."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    config = uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=port, log_level="critical", lifespan=lifespan
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), "server didn't start"
        time.sleep(0.05)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def database_down(settings: Settings) -> Iterator[httpx.Client]:
    """A server whose database refuses connections (nothing listens on port 1)."""
    with serve(
        settings.with_overrides(database_url="postgresql+psycopg://postgres@127.0.0.1:1/booking"),
        lifespan="off",  # startup would need the database
    ) as client:
        yield client


def test_a_locked_table_fails_fast_with_503(
    settings: Settings, engine: Engine, new_event, user: str
) -> None:
    event_id, _ = new_event(2)
    with serve(settings.with_overrides(db_lock_timeout_ms=300)) as client:
        with engine.connect() as conn, conn.begin():
            conn.execute(text("LOCK TABLE seats IN ACCESS EXCLUSIVE MODE"))
            start = time.monotonic()
            response = client.get(f"/events/{event_id}/seats", params={"limit": 1})
            took = time.monotonic() - start
        assert response.status_code == 503, response.text
        assert response.headers["retry-after"] == "5"
        assert took < 5, f"took {took:.1f} s: the lock timeout didn't apply"
        # The lock is gone: the same request works again.
        assert client.get(f"/events/{event_id}/seats", params={"limit": 1}).status_code == 200


def test_every_connection_gets_the_time_limits(settings: Settings) -> None:
    app = create_app(
        settings.with_overrides(db_statement_timeout_ms=7_000, db_lock_timeout_ms=2_000)
    )
    with app.state.engine.connect() as conn:
        assert conn.scalar(text("SHOW statement_timeout")) == "7s"
        assert conn.scalar(text("SHOW lock_timeout")) == "2s"
    app.state.engine.dispose()


def test_a_database_outage_is_a_503_with_retry_after(database_down: httpx.Client) -> None:
    before = metric(database_down, "db_unavailable_total")
    start = time.monotonic()
    response = database_down.get("/events", params={"limit": 1})
    assert time.monotonic() - start < 5
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["retry-after"] == "5"
    assert "temporarily unavailable" in response.json()["detail"]
    assert "check your orders" not in response.json()["detail"]
    assert metric(database_down, "db_unavailable_total") == before + 1


def test_an_order_during_an_outage_says_to_check_orders(database_down: httpx.Client) -> None:
    response = database_down.post(
        "/orders", headers={"X-API-Key": "key-user-1"}, json={"seat_ids": [1]}
    )
    assert response.status_code == 503
    assert "check your orders before trying again" in response.json()["detail"]


def test_readiness_follows_the_database(client: httpx.Client, database_down: httpx.Client) -> None:
    assert client.get("/ready").json() == {"status": "ready"}
    assert database_down.get("/ready").status_code == 503
    # Liveness is about the process, not its dependencies.
    assert database_down.get("/health").status_code == 200

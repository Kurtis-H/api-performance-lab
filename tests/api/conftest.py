"""Fixtures for API tests: a seeded Postgres database and a live server.

The server runs in a background thread with uvicorn, so tests talk to it over
real HTTP. That matters for the concurrency and stampede tests, which need
requests to actually overlap.

Which problems are switched on comes from PERF_BUGS, same as the app.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import httpx
import pytest
import uvicorn
from sqlalchemy import Engine, text

from app.config import Settings
from app.db import init_db, make_engine
from app.main import create_app
from app.seed import ADMIN_KEY, reset, seed, user_key

SEED_EVENTS = 20
SEED_SEATS = 50
SEED_USERS = 150
EXTRA_ADMIN_KEYS = [f"key-admin-{n}" for n in range(2, 7)]


class _Users:
    """Hands out user API keys so tests don't share a rate-limit bucket.

    Users 1-99 are handed out here. 100-150 are reserved for the
    concurrency test, which needs 50 buyers of its own.
    """

    def __init__(self) -> None:
        self._next = 1
        self._lock = threading.Lock()

    def key(self) -> str:
        with self._lock:
            n = self._next
            self._next += 1
        if n >= 100:
            raise RuntimeError("Ran out of test users; raise SEED_USERS")
        return user_key(n)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings.from_env().with_overrides(
        rate_limit_per_minute=120,
        payment_delay_ms=50,
        stats_delay_ms=100,
        stats_ttl_seconds=60.0,
        debug_buffer_size=100,
    )


@pytest.fixture(scope="session")
def engine(settings: Settings) -> Iterator[Engine]:
    eng = make_engine(settings.database_url)
    init_db(eng, with_seat_index=not settings.has("missing_index"))
    reset(eng)
    seed(eng, SEED_EVENTS, SEED_SEATS, SEED_USERS)
    with eng.begin() as conn:
        for key in EXTRA_ADMIN_KEYS:
            conn.execute(
                text("INSERT INTO users (name, api_key, is_admin) VALUES ('Admin', :k, true)"),
                {"k": key},
            )
    yield eng
    eng.dispose()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def base_url(settings: Settings, engine: Engine) -> Iterator[str]:
    app = create_app(settings)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("Test server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="session")
def client(base_url: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=base_url, timeout=30) as c:
        yield c


@pytest.fixture(scope="session")
def users() -> _Users:
    return _Users()


@pytest.fixture
def user(users: _Users) -> str:
    """A fresh user's API key."""
    return users.key()


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-API-Key": ADMIN_KEY}


@pytest.fixture
def new_event(
    client: httpx.Client, admin_headers: dict[str, str]
) -> Callable[..., tuple[int, list[int]]]:
    """Create an event through the admin API. Returns (event id, seat ids)."""

    def make(seats: int = 5, price_cents: int = 4000) -> tuple[int, list[int]]:
        created = client.post(
            "/admin/events",
            headers=admin_headers,
            json={
                "name": "Test event",
                "venue": "Test hall",
                "starts_at": datetime(2027, 6, 1, 19, tzinfo=UTC).isoformat(),
                "seats": seats,
                "price_cents": price_cents,
            },
        )
        assert created.status_code == 201, created.text
        event_id = created.json()["id"]
        page = client.get(f"/events/{event_id}/seats", params={"limit": 100})
        assert page.status_code == 200, page.text
        return event_id, [s["id"] for s in page.json()["items"]]

    return make


def metric(client: httpx.Client, name: str) -> float:
    """Read one unlabelled sample from /metrics."""
    for line in client.get("/metrics").text.splitlines():
        if line.startswith(name + " "):
            return float(line.split()[1])
    raise KeyError(name)

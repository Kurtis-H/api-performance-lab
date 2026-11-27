"""Runtime settings, including which performance problems are switched on.

Every problem has a broken and a fixed version in the same codebase. Pick the
broken ones with PERF_BUGS, a comma-separated list (or "all"):

    PERF_BUGS=n_plus_one,race uvicorn app.main:app

With PERF_BUGS unset, the app runs with every fix in place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

KNOWN_BUGS: dict[str, str] = {
    "n_plus_one": "GET /events runs one extra seat-count query per event on the page",
    "missing_index": "No index on seats(event_id, status), so seat lookups scan the whole table",
    "memory_leak": "Every seat map served is kept in an unbounded debug buffer",
    "no_page_limit": "List endpoints accept any limit and return everything by default",
    "race": "Orders check seats then write later, so two buyers can get the same seat",
    "cache_stampede": "When the stats cache expires, every waiting request recomputes it",
}

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/booking"


def parse_bugs(raw: str | None) -> frozenset[str]:
    """Turn the PERF_BUGS value into a set, rejecting names we don't know."""
    if not raw or not raw.strip():
        return frozenset()
    if raw.strip().lower() == "all":
        return frozenset(KNOWN_BUGS)
    names = {part.strip().lower() for part in raw.split(",") if part.strip()}
    unknown = names - KNOWN_BUGS.keys()
    if unknown:
        raise ValueError(
            f"Unknown PERF_BUGS value(s): {', '.join(sorted(unknown))}. "
            f"Known: {', '.join(sorted(KNOWN_BUGS))}, or 'all'."
        )
    return frozenset(names)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else int(raw)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else float(raw)


@dataclass(frozen=True)
class Settings:
    bugs: frozenset[str] = field(default_factory=frozenset)
    database_url: str = DEFAULT_DATABASE_URL
    # Requests per minute per API key on authenticated endpoints. 0 turns it off.
    rate_limit_per_minute: int = 60
    # Failed authentications per minute per client address before every key
    # from it is refused with 429 (pentest BP-03). 0 turns it off.
    auth_failures_per_minute: int = 30
    # Seats one buyer may hold for one event, across all their orders
    # (pentest BP-02). 0 turns it off.
    max_seats_per_buyer_per_event: int = 10
    # Stand-in for the call to a payment provider while an order is placed.
    payment_delay_ms: int = 50
    # Stand-in for a heavy reporting query behind GET /events/{id}/stats.
    stats_delay_ms: int = 100
    stats_ttl_seconds: float = 5.0
    # How many recent seat maps the debug buffer keeps when the leak is fixed.
    debug_buffer_size: int = 100
    db_pool_size: int = 20
    db_max_overflow: int = 30
    # Time limits on every database call (resilience-lab RL-01): a statement,
    # or a wait for a lock, that runs past them fails with 503 instead of
    # holding a worker thread and a connection until it finishes.
    db_statement_timeout_ms: int = 10_000
    db_lock_timeout_ms: int = 3_000
    db_connect_timeout_s: int = 5

    def has(self, bug: str) -> bool:
        if bug not in KNOWN_BUGS:
            raise KeyError(bug)
        return bug in self.bugs

    def with_overrides(self, **changes: object) -> Settings:
        return replace(self, **changes)  # type: ignore[arg-type]

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            bugs=parse_bugs(os.environ.get("PERF_BUGS")),
            database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            rate_limit_per_minute=_int_env("RATE_LIMIT_PER_MINUTE", 60),
            auth_failures_per_minute=_int_env("AUTH_FAILURES_PER_MINUTE", 30),
            max_seats_per_buyer_per_event=_int_env("MAX_SEATS_PER_BUYER", 10),
            payment_delay_ms=_int_env("PAYMENT_DELAY_MS", 50),
            stats_delay_ms=_int_env("STATS_DELAY_MS", 100),
            stats_ttl_seconds=_float_env("STATS_TTL_SECONDS", 5.0),
            debug_buffer_size=_int_env("DEBUG_BUFFER_SIZE", 100),
            db_pool_size=_int_env("DB_POOL_SIZE", 20),
            db_max_overflow=_int_env("DB_MAX_OVERFLOW", 30),
            db_statement_timeout_ms=_int_env("DB_STATEMENT_TIMEOUT_MS", 10_000),
            db_lock_timeout_ms=_int_env("DB_LOCK_TIMEOUT_MS", 3_000),
            db_connect_timeout_s=_int_env("DB_CONNECT_TIMEOUT_S", 5),
        )

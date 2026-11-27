"""Load deterministic test data.

    python -m app.seed --events 200 --seats 500 --users 200

API keys are predictable on purpose (``key-admin``, ``key-user-1`` ...) so the
load tests and the Postman collection can log in. This is lab data, not real
credentials: a real deployment must issue random keys (booking-pentest BP-01,
accepted for the lab). Only their SHA-256 hashes are stored.
"""

from __future__ import annotations

import argparse

from sqlalchemy import Engine, text

from app.config import Settings
from app.db import init_db, make_engine
from app.keys import hash_key

ADMIN_KEY = "key-admin"


def user_key(n: int) -> str:
    return f"key-user-{n}"


def reset(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("TRUNCATE order_items, orders, seats, events, users RESTART IDENTITY CASCADE")
        )


def seed(engine: Engine, events: int, seats_per_event: int, users: int) -> None:
    """Insert users, events and seats in a few set-based statements."""
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (name, api_key_hash, is_admin) VALUES ('Admin', :key, true)"),
            {"key": hash_key(ADMIN_KEY)},
        )
        conn.execute(
            text(
                "INSERT INTO users (name, api_key_hash, is_admin) "
                "SELECT 'User ' || g, "
                "encode(sha256(convert_to('key-user-' || g, 'UTF8')), 'hex'), false "
                "FROM generate_series(1, :n) AS g"
            ),
            {"n": users},
        )
        conn.execute(
            text(
                "INSERT INTO events (name, venue, starts_at) "
                "SELECT 'Event ' || g, 'Venue ' || (g % 12 + 1), "
                "       TIMESTAMPTZ '2027-01-01 19:00+00' + (g || ' days')::interval "
                "FROM generate_series(1, :n) AS g"
            ),
            {"n": events},
        )
        # Prices vary by row so revenue reports have something to add up.
        conn.execute(
            text(
                "INSERT INTO seats (event_id, label, price_cents, status) "
                "SELECT e.id, 'R' || ((g - 1) / 20 + 1) || '-' || ((g - 1) % 20 + 1), "
                "       2500 + ((g - 1) / 20) * 500, 'available' "
                "FROM events e CROSS JOIN generate_series(1, :n) AS g"
            ),
            {"n": seats_per_event},
        )


def warm(engine: Engine) -> None:
    """Make a freshly loaded table behave like one that has been running a while.

    VACUUM sets the visibility map. Until it runs, an index-only scan still has
    to visit the table for every row, so the event list (which counts ~20,000
    seats per page) ran about 30x slower until autovacuum got round to the new
    table. Load tests started straight after seeding were slow or fast
    depending on that timing. See docs/findings.md.
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM ANALYZE"))
        conn.execute(text("SELECT count(*) FROM seats"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Reset and seed the booking database.")
    parser.add_argument("--events", type=int, default=200)
    parser.add_argument("--seats", type=int, default=500, help="seats per event")
    parser.add_argument("--users", type=int, default=200)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    init_db(engine, with_seat_index=not settings.has("missing_index"))
    reset(engine)
    seed(engine, args.events, args.seats, args.users)
    warm(engine)
    print(
        f"Seeded {args.events} events x {args.seats} seats "
        f"({args.events * args.seats} seats) and {args.users} users."
    )


if __name__ == "__main__":
    main()

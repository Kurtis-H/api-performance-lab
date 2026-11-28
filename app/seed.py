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
            text(
                "TRUNCATE promo_codes, order_products, products, seat_holds, order_items, orders, "
                "seats, events, users "
                "RESTART IDENTITY CASCADE"
            )
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
        # Rows of 20. Prices vary by row so revenue reports have something to
        # add up, and the front rows form an Orchestra section, the next a
        # Mezzanine and the rest a Balcony, like a real venue.
        conn.execute(
            text(
                "INSERT INTO seats "
                "(event_id, label, price_cents, status, section, seat_row, seat_number) "
                "SELECT e.id, 'R' || ((g - 1) / 20 + 1) || '-' || ((g - 1) % 20 + 1), "
                "       2500 + ((g - 1) / 20) * 500, 'available', "
                "       CASE WHEN (g - 1) / 20 < 4 THEN 'Orchestra' "
                "            WHEN (g - 1) / 20 < 8 THEN 'Mezzanine' ELSE 'Balcony' END, "
                "       'R' || ((g - 1) / 20 + 1), (g - 1) % 20 + 1 "
                "FROM events e CROSS JOIN generate_series(1, :n) AS g"
            ),
            {"n": seats_per_event},
        )
    seed_shop(engine)


# Promo codes a demo can use. EXPIRED is there to try out the error.
PROMO_CODES = [
    {"code": "WELCOME10", "percent_off": 10, "applies_to": "all"},
    {"code": "SEATS20", "percent_off": 20, "applies_to": "seats", "max_uses": 500},
    {"code": "SHOP5", "amount_off_cents": 500, "applies_to": "products", "min_total_cents": 2000},
    {"code": "EXPIRED", "percent_off": 50, "expires_at": "2026-01-01T00:00:00+00:00"},
]


def seed_shop(engine: Engine) -> None:
    """Add-ons for every event, a little merch, and the demo promo codes."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO products "
                "(event_id, name, description, category, price_cents, stock, max_per_order) "
                "SELECT e.id, v.name, v.description, v.category, v.price, v.stock, v.max "
                "FROM events e CROSS JOIN (VALUES "
                "  ('Parking', 'One car, in the venue lot', 'parking', 1500, 40, 2), "
                "  ('Drink voucher', 'Any drink at the bar', 'food', 800, NULL, 10), "
                "  ('Program', 'The printed program', 'merch', 500, 100, 4)"
                ") AS v(name, description, category, price, stock, max) "
                "ORDER BY e.id"
            )
        )
        conn.execute(
            text(
                "INSERT INTO products "
                "(event_id, name, description, category, price_cents, stock, max_per_order) "
                "VALUES "
                "(NULL, 'Tour T-shirt', 'Soft cotton, one size', 'merch', 2500, 50, 4), "
                "(NULL, 'Poster', 'A2, signed by the house band', 'merch', 1200, 100, 4), "
                "(NULL, 'Tote bag', 'Canvas, with the season line-up', 'merch', 1800, NULL, 4)"
            )
        )
        for promo in PROMO_CODES:
            conn.execute(
                text(
                    "INSERT INTO promo_codes "
                    "(code, percent_off, amount_off_cents, applies_to, min_total_cents, "
                    " max_uses, uses, expires_at) "
                    "VALUES (:code, :percent_off, :amount_off_cents, :applies_to, "
                    " :min_total_cents, :max_uses, 0, CAST(:expires_at AS timestamptz))"
                ),
                {
                    "percent_off": None,
                    "amount_off_cents": None,
                    "applies_to": "all",
                    "min_total_cents": 0,
                    "max_uses": None,
                    "expires_at": None,
                    **promo,
                },
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

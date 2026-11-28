"""Database models, engine setup and a per-request query counter."""

from __future__ import annotations

import contextvars
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Engine,
    ForeignKey,
    Index,
    Integer,
    String,
    create_engine,
    event,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

SEAT_LOOKUP_INDEX = "ix_seats_event_status"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # SHA-256 of the key, hex; the key itself is never stored (app/keys.py).
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    venue: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Seat(Base):
    __tablename__ = "seats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # No index=True here on purpose: the lookup index is managed in init_db so the
    # missing_index problem can drop it. Postgres does not index foreign keys itself.
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(20))
    price_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="available")
    # Where the seat is: section (a price tier), row and number. Columns are
    # seat_row / seat_number because ROW is an SQL keyword.
    section: Mapped[str] = mapped_column(String(40), default="General", server_default="General")
    row: Mapped[str] = mapped_column("seat_row", String(8), default="", server_default="")
    number: Mapped[int | None] = mapped_column("seat_number", Integer, nullable=True)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # What was paid: the seats and products, less any discount.
    total_cents: Mapped[int] = mapped_column(Integer)
    discount_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    promo_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Product(Base):
    """Something sold besides seats: an add-on for one event, or merch (no event)."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(20))
    price_cents: Mapped[int] = mapped_column(Integer)
    # How many are left; None means there's no limit.
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_per_order: Mapped[int] = mapped_column(Integer, default=10)


class OrderProduct(Base):
    __tablename__ = "order_products"
    __table_args__ = (Index("ix_order_products_order", "order_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    # The unit price paid, so later price changes don't rewrite old orders.
    price_cents: Mapped[int] = mapped_column(Integer)


class PromoCode(Base):
    """A discount: a percentage or a fixed amount off, on seats, products or both."""

    __tablename__ = "promo_codes"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    percent_off: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_off_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applies_to: Mapped[str] = mapped_column(String(10), default="all")
    min_total_cents: Mapped[int] = mapped_column(Integer, default=0)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SeatHold(Base):
    """A buyer holding a seat for a while, so no one else can buy it.

    A hold ends when it expires (no clean-up job needed: an expired row is
    simply ignored), when its buyer releases it, or when the seat is sold. Kept
    out of the seats table so counting available seats stays an index-only
    scan; holds are few, so subtracting them is cheap.
    """

    __tablename__ = "seat_holds"
    __table_args__ = (Index("ix_seat_holds_event", "event_id", "expires_at"),)

    seat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("seats.id", ondelete="CASCADE"), primary_key=True
    )
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (Index("ix_order_items_order", "order_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    seat_id: Mapped[int] = mapped_column(ForeignKey("seats.id"))
    price_cents: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------- query counting


class QueryCounter:
    """Counts SQL statements. One is attached to each HTTP request."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0


current_counter: contextvars.ContextVar[QueryCounter | None] = contextvars.ContextVar(
    "current_counter", default=None
)


def _count_query(*_args: object, **_kwargs: object) -> None:
    counter = current_counter.get()
    if counter is not None:
        counter.count += 1


def make_engine(
    url: str,
    pool_size: int = 20,
    max_overflow: int = 30,
    *,
    statement_timeout_ms: int = 0,
    lock_timeout_ms: int = 0,
    connect_timeout_s: int = 0,
) -> Engine:
    """The engine, with optional time limits on every connection (0 = none).

    The limits are set per connection by Postgres itself (`options`), so they
    cost no extra queries.
    """
    options = []
    if statement_timeout_ms:
        options.append(f"-c statement_timeout={statement_timeout_ms}")
    if lock_timeout_ms:
        options.append(f"-c lock_timeout={lock_timeout_ms}")
    connect_args: dict[str, object] = {}
    if options:
        connect_args["options"] = " ".join(options)
    if connect_timeout_s:
        connect_args["connect_timeout"] = connect_timeout_s
    engine = create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    event.listen(engine, "before_cursor_execute", _count_query)
    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def _hash_stored_keys(engine: Engine) -> None:
    """Migrate a database from before keys were hashed (booking-pentest BP-06).

    Hashes each stored key in place and drops the plain-text column, so an
    existing database keeps working without a reseed and keeps no plain keys.
    """
    with engine.begin() as conn:
        columns = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'users'"
                )
            )
        }
        if "api_key" not in columns:
            return
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(64)"))
        conn.execute(
            text(
                "UPDATE users SET api_key_hash = "
                "encode(sha256(convert_to(api_key, 'UTF8')), 'hex') WHERE api_key_hash IS NULL"
            )
        )
        conn.execute(text("ALTER TABLE users ALTER COLUMN api_key_hash SET NOT NULL"))
        conn.execute(text("ALTER TABLE users DROP COLUMN api_key"))
        conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_api_key_hash ON users (api_key_hash)")
        )


def _add_seat_layout(engine: Engine) -> None:
    """Migrate a database from before venue layouts: add section, row and number.

    Existing seats are labelled "<row>-<number>" (R3-12, S-7), so the row and
    number are read back out of the label; their section is "General".
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE seats "
                "ADD COLUMN IF NOT EXISTS section VARCHAR(40) NOT NULL DEFAULT 'General', "
                "ADD COLUMN IF NOT EXISTS seat_row VARCHAR(8) NOT NULL DEFAULT '', "
                "ADD COLUMN IF NOT EXISTS seat_number INTEGER"
            )
        )
        conn.execute(
            text(
                "UPDATE seats SET seat_row = split_part(label, '-', 1), "
                "seat_number = NULLIF("
                "regexp_replace(split_part(label, '-', 2), '[^0-9]', '', 'g'), '')::int "
                "WHERE seat_row = '' AND position('-' in label) > 0"
            )
        )


def _add_order_discounts(engine: Engine) -> None:
    """Migrate a database from before the shop: orders gain a discount and a promo code."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE orders "
                "ADD COLUMN IF NOT EXISTS discount_cents INTEGER NOT NULL DEFAULT 0, "
                "ADD COLUMN IF NOT EXISTS promo_code VARCHAR(20)"
            )
        )


def init_db(engine: Engine, with_seat_index: bool = True) -> None:
    """Create tables, run the in-place migrations, then add or drop the seat index."""
    Base.metadata.create_all(engine)
    _hash_stored_keys(engine)
    _add_seat_layout(engine)
    _add_order_discounts(engine)
    with engine.begin() as conn:
        if with_seat_index:
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {SEAT_LOOKUP_INDEX} "
                    "ON seats (event_id, status, id)"
                )
            )
        else:
            conn.execute(text(f"DROP INDEX IF EXISTS {SEAT_LOOKUP_INDEX}"))
        conn.execute(text("ANALYZE seats"))

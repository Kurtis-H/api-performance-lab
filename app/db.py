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


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    total_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


def make_engine(url: str, pool_size: int = 20, max_overflow: int = 30) -> Engine:
    engine = create_engine(url, pool_size=pool_size, max_overflow=max_overflow, pool_pre_ping=True)
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


def init_db(engine: Engine, with_seat_index: bool = True) -> None:
    """Create tables, migrate stored keys to hashes, then add or drop the seat index."""
    Base.metadata.create_all(engine)
    _hash_stored_keys(engine)
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

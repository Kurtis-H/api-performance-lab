"""Booking API: events, seats and orders, with switchable performance problems.

Search this file for ``settings.has(`` to see each problem next to its fix.
"""

import functools
import time
from collections import Counter as Tally
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, ParamSpec, TypeVar

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import APIKeyHeader
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

from app.cache import TTLCache
from app.config import Settings
from app.db import (
    Event,
    Order,
    OrderItem,
    QueryCounter,
    Seat,
    User,
    current_counter,
    init_db,
    make_engine,
    make_sessionmaker,
)
from app.keys import hash_key
from app.ratelimit import RateLimiter
from app.schemas import (
    MAX_JSON_INT,
    ROW_LETTERS,
    EventCreated,
    EventIn,
    EventOut,
    EventPage,
    EventStats,
    HealthOut,
    OrderIn,
    OrderItemOut,
    OrderOut,
    OrderPage,
    Problem,
    SeatOut,
    SeatPage,
    SimpleEventIn,
)

MAX_PAGE = 100
DEFAULT_PAGE = 20
MAX_ID = 2**31 - 1  # ids are Postgres integers; larger values would be a 500

# ------------------------------------------------------------------- metrics
# Module level so creating several apps in one process (tests) doesn't
# register the same metric twice.

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
DB_QUERIES = Histogram(
    "db_queries_per_request",
    "SQL statements run per request",
    ["route"],
    buckets=(1, 2, 3, 5, 10, 20, 50, 100, 250),
)
STATS_RECOMPUTES = Counter("stats_recomputes_total", "Times the event stats report was computed")
DEBUG_BUFFER_ENTRIES = Gauge("debug_buffer_entries", "Seat maps held in the debug buffer")
ORDERS_CREATED = Counter("orders_created_total", "Orders placed")
ORDER_CONFLICTS = Counter("order_conflicts_total", "Orders refused because a seat was taken")
RATE_LIMITED = Counter("rate_limited_total", "Requests refused by the rate limiter")
AUTH_THROTTLED = Counter(
    "auth_throttled_total", "Requests refused after too many failed authentications"
)
BUYER_CAP_HIT = Counter("buyer_cap_refusals_total", "Orders refused by the per-buyer seat cap")
DB_UNAVAILABLE = Counter(
    "db_unavailable_total", "Requests answered 503 because the database failed or timed out"
)

# Database failures that say nothing about the request itself: the database
# is unreachable, dropped the connection, or hit a time limit (resilience-lab
# RL-01, RL-02). They're answered 503 with Retry-After, not 500.
DATABASE_UNAVAILABLE_ERRORS = (
    sa_exc.OperationalError,
    sa_exc.InterfaceError,
    sa_exc.DisconnectionError,
    sa_exc.TimeoutError,
)
DB_RETRY_AFTER_SECONDS = 5

AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": Problem, "description": "Missing or unknown API key"},
    429: {"model": Problem, "description": "Rate limit exceeded"},
}
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": Problem}}
BAD_BODY: dict[int | str, dict[str, Any]] = {
    400: {"model": Problem, "description": "Body is not valid JSON"}
}

EventId = Annotated[int, Path(ge=1, le=MAX_ID)]
OrderId = Annotated[int, Path(ge=1, le=MAX_JSON_INT)]


@dataclass(frozen=True)
class AuthUser:
    id: int
    is_admin: bool


P = ParamSpec("P")
R = TypeVar("R")


def releases_connection(endpoint: Callable[P, R]) -> Callable[P, R]:
    """Close the request's session the moment the endpoint returns.

    FastAPI runs sync endpoints and the cleanup of sync dependencies as
    separate jobs on a shared 40-thread pool. If the session were only closed
    in the dependency's cleanup, a finished request would keep its database
    connection while queueing for a thread to run that cleanup. Under a spike
    every thread ends up waiting for a connection and every connection is held
    by a request waiting for a thread: the pool deadlocks until it times out.
    The spike test found this (see docs/findings.md). Rule: never
    hold a connection while waiting for a thread.
    """

    @functools.wraps(endpoint)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return endpoint(*args, **kwargs)
        finally:
            session = kwargs.get("session")
            if isinstance(session, Session):
                session.close()

    return wrapper


@dataclass(frozen=True)
class Paging:
    limit: int | None
    offset: int


def make_paging_dependency(enforce_limit: bool) -> Callable[..., Paging]:
    if enforce_limit:

        def paging(
            limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
            offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
        ) -> Paging:
            return Paging(limit, offset)

    else:
        # Problem: no_page_limit. Any limit is accepted and leaving it out
        # returns every row.
        def paging(  # type: ignore[misc]
            limit: Annotated[int | None, Query(ge=1)] = None,
            offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
        ) -> Paging:
            return Paging(limit, offset)

    return paging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine: Engine = make_engine(
        settings.database_url,
        settings.db_pool_size,
        settings.db_max_overflow,
        statement_timeout_ms=settings.db_statement_timeout_ms,
        lock_timeout_ms=settings.db_lock_timeout_ms,
        connect_timeout_s=settings.db_connect_timeout_s,
    )
    make_session: sessionmaker = make_sessionmaker(engine)
    limiter = RateLimiter(settings.rate_limit_per_minute)
    # Failed authentications, counted per client address (pentest BP-03).
    auth_failures = RateLimiter(settings.auth_failures_per_minute)
    stats_cache: TTLCache[EventStats] = TTLCache(
        settings.stats_ttl_seconds, single_flight=not settings.has("cache_stampede")
    )
    # Problem: memory_leak. The buffer was meant to hold "the last few" seat maps
    # for debugging, but as a plain list it keeps every one ever served.
    debug_buffer: list[Any] | deque[Any] = (
        [] if settings.has("memory_leak") else deque(maxlen=settings.debug_buffer_size)
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        init_db(engine, with_seat_index=not settings.has("missing_index"))
        yield
        engine.dispose()

    app = FastAPI(
        title="Booking API",
        version="1.0.0",
        description="Event booking API used as a performance and API testing lab.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.stats_cache = stats_cache
    app.state.debug_buffer = debug_buffer
    app.state.limiter = limiter
    app.state.auth_failures = auth_failures

    async def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
        DB_UNAVAILABLE.inc()
        detail = "The booking service is temporarily unavailable. Please try again shortly."
        if request.method == "POST" and request.url.path == "/orders":
            # The failure may have come after the order was saved: say so.
            detail += " If you were placing an order, check your orders before trying again."
        return JSONResponse(
            {"detail": detail},
            status_code=503,
            headers={"Retry-After": str(DB_RETRY_AFTER_SECONDS)},
        )

    # Handlers are looked up by exception class, so each one is registered.
    for error in DATABASE_UNAVAILABLE_ERRORS:
        app.add_exception_handler(error, database_unavailable)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        headers = dict(exc.headers or {})
        if exc.status_code == 405:
            # Starlette only lists the methods of the first route that matched
            # the path. List every method this path supports.
            allowed: set[str] = set()
            for route in app.router.routes:
                if isinstance(route, APIRoute):
                    match, _ = route.matches(request.scope)
                    if match in (Match.FULL, Match.PARTIAL):
                        allowed |= route.methods or set()
            headers["Allow"] = ", ".join(sorted(allowed))
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)

    @app.middleware("http")
    async def measure(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        counter = QueryCounter()
        token = current_counter.set(counter)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            current_counter.reset(token)
        elapsed = time.perf_counter() - started
        route = getattr(request.scope.get("route"), "path", "unmatched")
        if route != "/metrics":
            HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
            HTTP_LATENCY.labels(request.method, route).observe(elapsed)
            DB_QUERIES.labels(route).observe(counter.count)
        response.headers["X-DB-Queries"] = str(counter.count)
        if "x-api-key" in request.headers:
            # A response to an authenticated request is that buyer's data: never
            # let a browser or a shared proxy keep a copy (pentest BP-08).
            response.headers["Cache-Control"] = "no-store"
        return response

    def get_session() -> Iterator[Session]:
        session = make_session()
        try:
            yield session
        finally:
            session.close()  # rolls back anything not committed

    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def current_user(
        request: Request,
        key: Annotated[str | None, Depends(api_key_header)],
    ) -> AuthUser:
        if not key:
            raise HTTPException(401, "Missing X-API-Key header")
        # A client that keeps presenting wrong keys is refused before the lookup,
        # so it can't keep guessing at full speed (pentest BP-03). Checked first:
        # answering 401 vs 200 after the limit would still confirm a guess.
        client = request.client.host if request.client else "unknown"
        blocked = auth_failures.peek(client)
        if blocked is not None:
            AUTH_THROTTLED.inc()
            raise HTTPException(
                429,
                "Too many failed sign-ins; try again later",
                headers={"Retry-After": RateLimiter.retry_after_header(blocked)},
            )
        # A short session of its own, closed before this function returns, so
        # no connection is held while the request waits for its next thread.
        with make_session() as session:
            found = session.scalar(select(User).where(User.api_key_hash == hash_key(key)))
            user = None if found is None else AuthUser(found.id, found.is_admin)
        if user is None:
            auth_failures.check(client)
            raise HTTPException(401, "Unknown API key")
        wait = limiter.check(key)
        if wait is not None:
            RATE_LIMITED.inc()
            raise HTTPException(
                429,
                "Rate limit exceeded",
                headers={"Retry-After": RateLimiter.retry_after_header(wait)},
            )
        return user

    def admin_user(user: Annotated[AuthUser, Depends(current_user)]) -> AuthUser:
        if not user.is_admin:
            raise HTTPException(403, "Admin only")
        return user

    paging_dep = make_paging_dependency(enforce_limit=not settings.has("no_page_limit"))
    SessionDep = Annotated[Session, Depends(get_session)]
    PagingDep = Annotated[Paging, Depends(paging_dep)]
    UserDep = Annotated[AuthUser, Depends(current_user)]

    # ------------------------------------------------------------- endpoints

    @app.get("/health", response_model=HealthOut, tags=["ops"])
    def health() -> HealthOut:
        return HealthOut(status="ok", bugs=sorted(settings.bugs))

    @app.get("/ready", include_in_schema=False)
    def ready() -> dict[str, str]:
        """Readiness: can this instance serve requests? (resilience-lab RL-03)

        /health is liveness and stays 200 while the process runs. This checks
        the database too, so a load balancer can stop sending traffic while
        it's unreachable; the error handler above turns a failure into 503.
        """
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        DEBUG_BUFFER_ENTRIES.set(len(debug_buffer))
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/events", response_model=EventPage, tags=["events"])
    @releases_connection
    def list_events(session: SessionDep, page: PagingDep) -> EventPage:
        total = session.scalar(text("SELECT count(*) FROM events")) or 0

        if settings.has("n_plus_one"):
            # Problem: n_plus_one. One query for the page, then one more per event.
            rows = session.execute(
                select(Event).order_by(Event.id).limit(page.limit).offset(page.offset)
            ).scalars()
            items = []
            for event in rows:
                available = session.scalar(
                    text(
                        "SELECT count(*) FROM seats WHERE event_id = :id AND status = 'available'"
                    ),
                    {"id": event.id},
                )
                items.append(_event_out(event, available or 0))
        else:
            # Fix: count seats for the whole page in the same query.
            result = session.execute(
                text(
                    "SELECT e.id, e.name, e.venue, e.starts_at, "
                    "       count(s.id) AS available "
                    "FROM (SELECT * FROM events ORDER BY id LIMIT :limit OFFSET :offset) e "
                    "LEFT JOIN seats s ON s.event_id = e.id AND s.status = 'available' "
                    "GROUP BY e.id, e.name, e.venue, e.starts_at ORDER BY e.id"
                ),
                {"limit": page.limit, "offset": page.offset},
            )
            items = [
                EventOut(
                    id=r.id,
                    name=r.name,
                    venue=r.venue,
                    starts_at=r.starts_at,
                    available_seats=r.available,
                )
                for r in result
            ]
        return EventPage(items=items, total=total, limit=page.limit, offset=page.offset)

    @app.get("/events/{event_id}", response_model=EventOut, responses=NOT_FOUND, tags=["events"])
    @releases_connection
    def get_event(event_id: EventId, session: SessionDep) -> EventOut:
        event = _require_event(session, event_id)
        available = session.scalar(
            text("SELECT count(*) FROM seats WHERE event_id = :id AND status = 'available'"),
            {"id": event_id},
        )
        return _event_out(event, available or 0)

    @app.get(
        "/events/{event_id}/seats",
        response_model=SeatPage,
        responses=NOT_FOUND,
        tags=["events"],
    )
    @releases_connection
    def list_seats(
        event_id: EventId,
        session: SessionDep,
        page: PagingDep,
        status: Annotated[str, Query(pattern="^(available|sold)$")] = "available",
    ) -> SeatPage:
        _require_event(session, event_id)
        seats = session.execute(
            select(Seat)
            .where(Seat.event_id == event_id, Seat.status == status)
            .order_by(Seat.id)
            .limit(page.limit)
            .offset(page.offset)
        ).scalars()
        items = [
            SeatOut(
                id=s.id,
                label=s.label,
                price_cents=s.price_cents,
                status=s.status,
                section=s.section,
                row=s.row,
                number=s.number,
            )
            for s in seats
        ]
        debug_buffer.append({"event_id": event_id, "seats": [i.model_dump() for i in items]})
        return SeatPage(items=items, limit=page.limit, offset=page.offset)

    @app.get(
        "/events/{event_id}/stats",
        response_model=EventStats,
        responses=NOT_FOUND,
        tags=["events"],
    )
    @releases_connection
    def event_stats(event_id: EventId, session: SessionDep) -> EventStats:
        # The cache is checked before touching the database, so a cache hit
        # never takes a pooled connection. (Checking the event first meant
        # every request waiting on a recompute held a connection, and a crowd
        # of 100 emptied the pool.) A 404 raised inside compute isn't cached.

        def compute() -> EventStats:
            _require_event(session, event_id)
            STATS_RECOMPUTES.inc()
            if settings.stats_delay_ms:
                session.execute(text("SELECT pg_sleep(:s)"), {"s": settings.stats_delay_ms / 1000})
            row = session.execute(
                text(
                    "SELECT count(*) AS total, "
                    "       count(*) FILTER (WHERE status = 'sold') AS sold, "
                    "       coalesce(sum(price_cents) FILTER (WHERE status = 'sold'), 0) "
                    "         AS revenue "
                    "FROM seats WHERE event_id = :id"
                ),
                {"id": event_id},
            ).one()
            return EventStats(
                event_id=event_id,
                seats_total=row.total,
                seats_sold=row.sold,
                revenue_cents=row.revenue,
            )

        return stats_cache.get_or_compute(event_id, compute)

    @app.post(
        "/orders",
        status_code=201,
        response_model=OrderOut,
        responses={
            **AUTH_RESPONSES,
            **BAD_BODY,
            404: {"model": Problem, "description": "A seat does not exist"},
            409: {"model": Problem, "description": "A seat is already taken"},
        },
        tags=["orders"],
    )
    @releases_connection
    def create_order(body: OrderIn, user: UserDep, session: SessionDep) -> OrderOut:
        seat_ids = sorted(set(body.seat_ids))
        if len(seat_ids) != len(body.seat_ids):
            raise HTTPException(409, "The same seat is listed twice")

        if settings.has("race"):
            # Problem: race. Read without locking, check, do slow work, then
            # write unconditionally. Two buyers can both pass the check.
            seats = list(session.execute(select(Seat).where(Seat.id.in_(seat_ids))).scalars())
            _check_seats(seats, seat_ids)
            _check_buyer_cap(session, user, seats, settings.max_seats_per_buyer_per_event)
            _charge_payment(settings)
            session.execute(update(Seat).where(Seat.id.in_(seat_ids)).values(status="sold"))
        else:
            # Fix: lock the rows (in id order, so overlapping orders can't
            # deadlock) before checking them. A second buyer waits here and then
            # sees the seat as sold.
            seats = list(
                session.execute(
                    select(Seat).where(Seat.id.in_(seat_ids)).order_by(Seat.id).with_for_update()
                ).scalars()
            )
            _check_seats(seats, seat_ids)
            _check_buyer_cap(session, user, seats, settings.max_seats_per_buyer_per_event)
            _charge_payment(settings)
            for seat in seats:
                seat.status = "sold"

        order = Order(user_id=user.id, total_cents=sum(s.price_cents for s in seats))
        session.add(order)
        session.flush()
        session.add_all(
            OrderItem(order_id=order.id, seat_id=s.id, price_cents=s.price_cents) for s in seats
        )
        session.commit()
        ORDERS_CREATED.inc()
        return OrderOut(
            id=order.id,
            total_cents=order.total_cents,
            created_at=order.created_at,
            items=[
                OrderItemOut(seat_id=s.id, label=s.label, price_cents=s.price_cents) for s in seats
            ],
        )

    @app.get("/orders", response_model=OrderPage, responses=AUTH_RESPONSES, tags=["orders"])
    @releases_connection
    def list_orders(
        user: UserDep,
        session: SessionDep,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    ) -> OrderPage:
        orders = list(
            session.execute(
                select(Order)
                .where(Order.user_id == user.id)
                .order_by(Order.id.desc())
                .limit(limit)
                .offset(offset)
            ).scalars()
        )
        items_by_order: dict[int, list[OrderItemOut]] = {o.id: [] for o in orders}
        if orders:
            # One query for every order's items, joined to their seats for the
            # labels (no extra query per item).
            for item, label in session.execute(
                select(OrderItem, Seat.label)
                .join(Seat, Seat.id == OrderItem.seat_id)
                .where(OrderItem.order_id.in_(items_by_order))
                .order_by(OrderItem.id)
            ):
                items_by_order[item.order_id].append(
                    OrderItemOut(seat_id=item.seat_id, label=label, price_cents=item.price_cents)
                )
        return OrderPage(
            items=[
                OrderOut(
                    id=o.id,
                    total_cents=o.total_cents,
                    created_at=o.created_at,
                    items=items_by_order[o.id],
                )
                for o in orders
            ],
            limit=limit,
            offset=offset,
        )

    @app.get(
        "/orders/{order_id}",
        response_model=OrderOut,
        responses={**AUTH_RESPONSES, **NOT_FOUND},
        tags=["orders"],
    )
    @releases_connection
    def get_order(order_id: OrderId, user: UserDep, session: SessionDep) -> OrderOut:
        # Scoped to the caller, so someone else's order id is a plain 404
        # (OWASP API1, broken object level authorization).
        order = session.scalar(select(Order).where(Order.id == order_id, Order.user_id == user.id))
        if order is None:
            raise HTTPException(404, "Order not found")
        items = session.execute(
            select(OrderItem, Seat.label)
            .join(Seat, Seat.id == OrderItem.seat_id)
            .where(OrderItem.order_id == order.id)
            .order_by(OrderItem.id)
        )
        return OrderOut(
            id=order.id,
            total_cents=order.total_cents,
            created_at=order.created_at,
            items=[
                OrderItemOut(seat_id=i.seat_id, label=label, price_cents=i.price_cents)
                for i, label in items
            ],
        )

    @app.post(
        "/admin/events",
        status_code=201,
        response_model=EventCreated,
        responses={
            **AUTH_RESPONSES,
            **BAD_BODY,
            403: {"model": Problem, "description": "Not an admin"},
        },
        tags=["admin"],
    )
    @releases_connection
    def create_event(
        body: EventIn,
        _admin: Annotated[AuthUser, Depends(admin_user)],
        session: SessionDep,
    ) -> EventCreated:
        event = Event(name=body.name, venue=body.venue, starts_at=body.starts_at)
        session.add(event)
        session.flush()
        layout = _layout(body)
        # One statement for every seat, however many sections: arrays unnested
        # side by side, so creating an event costs the same queries as before.
        session.execute(
            text(
                "INSERT INTO seats "
                "(event_id, label, price_cents, status, section, seat_row, seat_number) "
                "SELECT :event, l, p, 'available', s, r, n "
                "FROM unnest(CAST(:labels AS text[]), CAST(:prices AS int[]), "
                "CAST(:sections AS text[]), CAST(:rows AS text[]), CAST(:numbers AS int[])) "
                "AS t(l, p, s, r, n)"
            ),
            {
                "event": event.id,
                "labels": [f"{row}-{n}" for _, row, n, _ in layout],
                "prices": [price for *_, price in layout],
                "sections": [section for section, *_ in layout],
                "rows": [row for _, row, _, _ in layout],
                "numbers": [n for _, _, n, _ in layout],
            },
        )
        session.commit()
        return EventCreated(id=event.id, seats=len(layout))

    return app


# ------------------------------------------------------------------ helpers


def _layout(body: EventIn) -> list[tuple[str, str, int, int]]:
    """Every seat of a new event as (section, row, number, price), in seating order.

    Simple events are one row "S" in section "General" (labels S-1, S-2, ...).
    Layouts take row letters in order across sections, front to back.
    """
    if isinstance(body, SimpleEventIn):
        return [("General", "S", n, body.price_cents) for n in range(1, body.seats + 1)]
    seats = []
    letters = iter(ROW_LETTERS)
    for section in body.sections:
        for _ in range(section.rows):
            row = next(letters)
            seats += [
                (section.name, row, n, section.price_cents)
                for n in range(1, section.seats_per_row + 1)
            ]
    return seats


def _event_out(event: Event, available: int) -> EventOut:
    return EventOut(
        id=event.id,
        name=event.name,
        venue=event.venue,
        starts_at=event.starts_at,
        available_seats=available,
    )


def _require_event(session: Session, event_id: int) -> Event:
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "Event not found")
    return event


def _check_seats(seats: list[Seat], wanted: list[int]) -> None:
    if len(seats) != len(wanted):
        raise HTTPException(404, "One or more seats do not exist")
    if any(seat.status != "available" for seat in seats):
        ORDER_CONFLICTS.inc()
        raise HTTPException(409, "One or more seats are already taken")


def _check_buyer_cap(session: Session, user: AuthUser, seats: list[Seat], cap: int) -> None:
    """One buyer may hold at most `cap` seats per event, across orders (pentest BP-02).

    The buyer's row is locked first, so two of their orders running at once
    can't both pass the count. Seats are already locked, always before the
    buyer, so the lock order is the same for every order and can't deadlock.
    """
    if not cap:
        return
    session.execute(select(User.id).where(User.id == user.id).with_for_update())
    wanted = Tally(seat.event_id for seat in seats)
    rows = session.execute(
        select(Seat.event_id, func.count())
        .join(OrderItem, OrderItem.seat_id == Seat.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.user_id == user.id, Seat.event_id.in_(list(wanted)))
        .group_by(Seat.event_id)
    )
    held = {event: count for event, count in rows}
    if any(held.get(event, 0) + count > cap for event, count in wanted.items()):
        BUYER_CAP_HIT.inc()
        raise HTTPException(409, f"You can hold at most {cap} seats for one event")


def _charge_payment(settings: Settings) -> None:
    """Stand-in for a call to a payment provider."""
    if settings.payment_delay_ms:
        time.sleep(settings.payment_delay_ms / 1000)


app = create_app()

"""Request and response shapes. Requests reject unknown fields (mass assignment)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventOut(BaseModel):
    id: int
    name: str
    venue: str
    starts_at: datetime
    available_seats: int


class EventPage(BaseModel):
    items: list[EventOut]
    total: int
    limit: int | None
    offset: int


class SeatOut(BaseModel):
    id: int
    label: str
    price_cents: int
    status: str
    section: str = Field(description='The price tier the seat is in, such as "Orchestra".')
    row: str = Field(description='The row, such as "A". Rows run on across sections.')
    number: int | None = Field(description="The seat's number in its row.")


class SeatPage(BaseModel):
    items: list[SeatOut]
    limit: int | None
    offset: int


class EventStats(BaseModel):
    event_id: int
    seats_total: int
    seats_sold: int
    revenue_cents: int


# 2**53 is the largest integer every JSON parser holds exactly.
MAX_JSON_INT = 2**53


def _whole_number(value: object) -> object:
    """Accept 3 and 3.0 (JSON Schema treats both as integers); refuse true/false and "3".

    Pydantic's default would quietly turn true into 1 and "3" into 3.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("must be a whole number")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("must be a whole number")
        return int(value)
    return value


WholeNumber = BeforeValidator(_whole_number)
SeatId = Annotated[int, Field(ge=1, le=MAX_JSON_INT), WholeNumber]
# Postgres refuses NUL characters in text, so reject them up front.
SafeText = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00]+$")]


def _require_text(value: object) -> object:
    # Pydantic would otherwise read a bare number as a Unix timestamp.
    if not isinstance(value, str):
        raise ValueError("must be an ISO 8601 date-time string")
    return value


IsoDateTime = Annotated[datetime, BeforeValidator(_require_text)]


class OrderIn(StrictModel):
    seat_ids: list[SeatId] = Field(min_length=1, max_length=10)


class HoldIn(StrictModel):
    seat_ids: list[SeatId] = Field(min_length=1, max_length=10)


class HoldOut(BaseModel):
    event_id: int
    seat_ids: list[int] = Field(description="The seats you hold, in seating order.")
    expires_at: datetime = Field(description="When the hold ends and the seats are free again.")


class OrderItemOut(BaseModel):
    seat_id: int
    label: str = Field(description='The seat as buyers know it, such as "R3-12".')
    price_cents: int


class OrderOut(BaseModel):
    id: int
    total_cents: int
    created_at: datetime
    items: list[OrderItemOut]


class OrderPage(BaseModel):
    items: list[OrderOut]
    limit: int
    offset: int


MAX_SEATS_PER_EVENT = 1000
# Theater row letters: I and O are skipped, as they're easy to misread as 1 and 0.
ROW_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
MAX_SECTIONS = 4
MAX_ROWS_PER_SECTION = 6
MAX_SEATS_PER_ROW = 40
# The bounds are chosen so that every layout the spec allows fits: there are
# enough row letters, and no event goes over the seat limit. That keeps the
# OpenAPI spec the whole contract, with no hidden rules for clients to trip on.
assert len(ROW_LETTERS) >= MAX_SECTIONS * MAX_ROWS_PER_SECTION
assert MAX_SECTIONS * MAX_ROWS_PER_SECTION * MAX_SEATS_PER_ROW <= MAX_SEATS_PER_EVENT

Price = Annotated[int, Field(ge=0, le=1_000_000), WholeNumber]


class SectionIn(StrictModel):
    """One price tier of a venue: a block of rows, all at one price."""

    name: Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[^\x00]+$")]
    rows: Annotated[int, Field(ge=1, le=MAX_ROWS_PER_SECTION), WholeNumber]
    seats_per_row: Annotated[int, Field(ge=1, le=MAX_SEATS_PER_ROW), WholeNumber]
    price_cents: Price


class SimpleEventIn(StrictModel):
    """An event with one block of seats at one price, in row "S"."""

    name: SafeText
    venue: SafeText
    starts_at: IsoDateTime
    seats: Annotated[int, Field(ge=1, le=MAX_SEATS_PER_EVENT), WholeNumber]
    price_cents: Price


class LayoutEventIn(StrictModel):
    """An event with a venue layout: sections front to back, each a price tier.

    Row letters run on across sections, so the second section starts where the
    first ended.
    """

    name: SafeText
    venue: SafeText
    starts_at: IsoDateTime
    sections: Annotated[list[SectionIn], Field(min_length=1, max_length=MAX_SECTIONS)]


# Unknown fields are refused, so a body matches exactly one of the two shapes.
EventIn = SimpleEventIn | LayoutEventIn


class EventCreated(BaseModel):
    id: int
    seats: int


class Problem(BaseModel):
    detail: str


class HealthOut(BaseModel):
    status: str
    bugs: list[str]

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


PromoCodeText = Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{3,20}$")]
# Products and events have Postgres integer ids: anything larger can't exist,
# and sent to the database it would be an error (a 500), not a 404.
MAX_INT_ID = 2**31 - 1
ProductId = Annotated[int, Field(ge=1, le=MAX_INT_ID), WholeNumber]


class ProductLineIn(StrictModel):
    product_id: ProductId
    quantity: Annotated[int, Field(ge=1, le=10), WholeNumber]


class SeatOrderIn(StrictModel):
    """Seats, with any products alongside."""

    seat_ids: list[SeatId] = Field(min_length=1, max_length=10)
    products: list[ProductLineIn] = Field(default_factory=list, max_length=10)
    promo_code: PromoCodeText | None = None


class ShopOrderIn(StrictModel):
    """Products only, such as merch, with no seats."""

    seat_ids: list[SeatId] = Field(default_factory=list, max_length=0)
    products: list[ProductLineIn] = Field(min_length=1, max_length=10)
    promo_code: PromoCodeText | None = None


# An order has seats, products or both. Prices always come from the server:
# unknown fields such as price_cents are refused.
OrderIn = SeatOrderIn | ShopOrderIn


class HoldIn(StrictModel):
    seat_ids: list[SeatId] = Field(min_length=1, max_length=10)


class HoldOut(BaseModel):
    event_id: int
    seat_ids: list[int] = Field(description="The seats you hold, in seating order.")
    expires_at: datetime = Field(description="When the hold ends and the seats are free again.")


class ProductOut(BaseModel):
    id: int
    event_id: int | None = Field(description="The event this is an add-on for; null for merch.")
    name: str
    description: str
    category: str = Field(description='"parking", "food", "merch" and so on.')
    price_cents: int
    stock_left: int | None = Field(description="How many are left; null means no limit.")
    max_per_order: int


class ProductIn(StrictModel):
    event_id: Annotated[int, Field(ge=1, le=MAX_INT_ID), WholeNumber] | None = None
    name: Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[^\x00]+$")]
    description: Annotated[str, Field(max_length=200, pattern=r"^[^\x00]*$")] = ""
    category: Annotated[str, Field(pattern=r"^[a-z]{3,20}$")]
    price_cents: Price
    stock: Annotated[int, Field(ge=0, le=1_000_000), WholeNumber] | None = None
    max_per_order: Annotated[int, Field(ge=1, le=10), WholeNumber] = 10


class _PromoBase(StrictModel):
    code: Annotated[str, Field(pattern=r"^[A-Z0-9-]{3,20}$")]
    applies_to: Annotated[str, Field(pattern="^(all|seats|products)$")] = "all"
    min_total_cents: Annotated[int, Field(ge=0, le=10_000_000), WholeNumber] = 0
    max_uses: Annotated[int, Field(ge=1, le=1_000_000), WholeNumber] | None = None
    expires_at: IsoDateTime | None = None


class PercentPromoIn(_PromoBase):
    percent_off: Annotated[int, Field(ge=1, le=100), WholeNumber]


class AmountPromoIn(_PromoBase):
    amount_off_cents: Annotated[int, Field(ge=1, le=1_000_000), WholeNumber]


# A promo code takes a percentage or an amount off, never both.
PromoIn = PercentPromoIn | AmountPromoIn


class OrderProductOut(BaseModel):
    product_id: int
    name: str
    quantity: int
    price_cents: int = Field(description="The unit price paid.")


class Quote(BaseModel):
    """What an order would cost, before it's placed."""

    seats_cents: int
    products_cents: int
    subtotal_cents: int
    discount_cents: int
    total_cents: int
    promo_code: str | None


class OrderItemOut(BaseModel):
    seat_id: int
    label: str = Field(description='The seat as buyers know it, such as "R3-12".')
    price_cents: int


class OrderOut(BaseModel):
    id: int
    total_cents: int = Field(description="What was paid: seats and products, less any discount.")
    created_at: datetime
    items: list[OrderItemOut] = Field(description="The seats.")
    products: list[OrderProductOut] = Field(default_factory=list)
    discount_cents: int = 0
    promo_code: str | None = None


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

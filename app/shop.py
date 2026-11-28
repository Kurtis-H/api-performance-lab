"""The shop: products, stock and promo codes.

Quotes and orders price a basket with the same functions, so a quote never
disagrees with the order that follows it. Only orders lock rows and change
stock or promo uses; a quote just reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Order, OrderItem, Product, PromoCode, Seat
from app.schemas import ProductLineIn


@dataclass(frozen=True)
class Line:
    product: Product
    quantity: int

    @property
    def cents(self) -> int:
        return self.product.price_cents * self.quantity


@dataclass(frozen=True)
class Priced:
    seats_cents: int
    products_cents: int
    discount_cents: int
    promo: PromoCode | None

    @property
    def subtotal_cents(self) -> int:
        return self.seats_cents + self.products_cents

    @property
    def total_cents(self) -> int:
        return self.subtotal_cents - self.discount_cents


def dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def load_products(session: Session, wanted: list[ProductLineIn], *, lock: bool) -> list[Line]:
    """The products asked for, checked against their limits and stock.

    With `lock`, the rows are locked (in id order, so two orders can't
    deadlock) and the stock checked here can't change before the order commits.
    """
    if not wanted:
        return []
    ids = [line.product_id for line in wanted]
    if len(set(ids)) != len(ids):
        raise HTTPException(409, "The same product is listed twice")
    query = select(Product).where(Product.id.in_(ids)).order_by(Product.id)
    if lock:
        query = query.with_for_update()
    found = {product.id: product for product in session.scalars(query)}
    if len(found) != len(ids):
        raise HTTPException(404, "One or more products do not exist")
    lines = []
    for line in wanted:
        product = found[line.product_id]
        if line.quantity > product.max_per_order:
            raise HTTPException(
                409, f"You can buy at most {product.max_per_order} of {product.name} in one order"
            )
        if product.stock is not None and line.quantity > product.stock:
            if product.stock == 0:
                raise HTTPException(409, f"{product.name} is sold out")
            raise HTTPException(409, f"Only {product.stock} of {product.name} left")
        lines.append(Line(product, line.quantity))
    return lines


def check_add_ons(session: Session, user_id: int, lines: list[Line], seat_events: set[int]) -> None:
    """Add-ons (parking, drinks) are for people going: they need a seat at their
    event, in this order or one the buyer already has."""
    needed = {line.product.event_id for line in lines if line.product.event_id is not None}
    needed -= seat_events
    if not needed:
        return
    owned = set(
        session.scalars(
            select(Seat.event_id)
            .join(OrderItem, OrderItem.seat_id == Seat.id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.user_id == user_id, Seat.event_id.in_(needed))
            .distinct()
        )
    )
    if needed - owned:
        raise HTTPException(
            409, "Add-ons need a seat at their event, in this order or one you've already bought"
        )


def find_promo(session: Session, code: str | None, *, lock: bool) -> PromoCode | None:
    """The promo code, if it can be used now. Codes are case-insensitive.

    With `lock`, the row is locked until the order commits, so two orders can't
    both take a code's last use.
    """
    if code is None:
        return None
    query = select(PromoCode).where(PromoCode.code == code.upper())
    if lock:
        query = query.with_for_update()
    promo = session.scalar(query)
    if promo is None:
        raise HTTPException(409, "That promo code isn't valid")
    if promo.expires_at is not None and promo.expires_at <= datetime.now(UTC):
        raise HTTPException(409, "That promo code has expired")
    if promo.max_uses is not None and promo.uses >= promo.max_uses:
        raise HTTPException(409, "That promo code has been used up")
    return promo


def price(seats_cents: int, lines: list[Line], promo: PromoCode | None) -> Priced:
    """Add up the basket and apply the promo code.

    A percentage is rounded down to the cent, so a discount is never more than
    the percentage promised. A fixed amount is never more than what it applies to.
    """
    products_cents = sum(line.cents for line in lines)
    discount = 0
    if promo is not None:
        eligible = {
            "all": seats_cents + products_cents,
            "seats": seats_cents,
            "products": products_cents,
        }[promo.applies_to]
        if eligible == 0:
            what = "seats" if promo.applies_to == "seats" else "add-ons and merch"
            raise HTTPException(409, f"That promo code only applies to {what}")
        if seats_cents + products_cents < promo.min_total_cents:
            raise HTTPException(
                409, f"That promo code needs an order of at least {dollars(promo.min_total_cents)}"
            )
        if promo.percent_off is not None:
            discount = eligible * promo.percent_off // 100
        else:
            discount = min(promo.amount_off_cents or 0, eligible)
    return Priced(seats_cents, products_cents, discount, promo)

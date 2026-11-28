"""The shop: add-ons and merch, stock, and promo codes."""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest

from app.seed import user_key
from tests.api.conftest import SHOP_ADMIN_KEYS

NewEvent = Callable[..., tuple[int, list[int]]]
_admins = itertools.cycle(SHOP_ADMIN_KEYS)


def admin() -> dict[str, str]:
    return {"X-API-Key": next(_admins)}


def order(client: httpx.Client, key: str, **body: Any) -> httpx.Response:
    return client.post("/orders", headers={"X-API-Key": key}, json=body)


def quote(client: httpx.Client, key: str, **body: Any) -> httpx.Response:
    return client.post("/quote", headers={"X-API-Key": key}, json=body)


def add_ons(client: httpx.Client, event_id: int) -> dict[str, dict[str, Any]]:
    found = client.get(f"/events/{event_id}/products")
    assert found.status_code == 200, found.text
    return {p["name"]: p for p in found.json()}


def new_product(client: httpx.Client, **fields: Any) -> dict[str, Any]:
    created = client.post(
        "/admin/products",
        headers=admin(),
        json={"name": "Test item", "category": "merch", "price_cents": 1000, **fields},
    )
    assert created.status_code == 201, created.text
    return created.json()


_promo_counter = iter(range(1, 1_000_000))


def new_promo(client: httpx.Client, **fields: Any) -> str:
    code = f"TEST-{next(_promo_counter)}"
    created = client.post("/admin/promo-codes", headers=admin(), json={"code": code, **fields})
    assert created.status_code == 201, created.text
    return code


def line(product: dict[str, Any], quantity: int = 1) -> dict[str, int]:
    return {"product_id": product["id"], "quantity": quantity}


# --------------------------------------------------------------- the catalog


def test_every_event_has_add_ons_and_there_is_merch(client: httpx.Client, new_event) -> None:
    event_id, _ = new_event(seats=1)
    # Seeded events have add-ons; new events start without.
    assert add_ons(client, event_id) == {}
    seeded = add_ons(client, 1)
    # At least these: the contract fuzzer may add products to event 1 too.
    assert set(seeded) >= {"Parking", "Drink voucher", "Program"}
    assert seeded["Parking"] | {"id": 0} == {
        "id": 0,
        "event_id": 1,
        "name": "Parking",
        "description": "One car, in the venue lot",
        "category": "parking",
        "price_cents": 1500,
        "stock_left": 40,
        "max_per_order": 2,
    }
    merch = client.get("/products").json()
    assert {p["name"] for p in merch} >= {"Tour T-shirt", "Poster", "Tote bag"}
    assert all(p["event_id"] is None for p in merch)
    assert client.get("/events/999999/products").status_code == 404


# ------------------------------------------------------------------- buying


def test_seats_and_add_ons_in_one_order(client: httpx.Client, user: str, new_event) -> None:
    event_id, seats = new_event(seats=2, price_cents=4000)
    drinks = new_product(client, event_id=event_id, name="Drink voucher", price_cents=800)

    placed = order(client, user, seat_ids=seats[:1], products=[line(drinks, 3)])
    assert placed.status_code == 201, placed.text
    body = placed.json()
    assert body["total_cents"] == 4000 + 3 * 800
    assert body["discount_cents"] == 0 and body["promo_code"] is None
    assert body["products"] == [
        {"product_id": drinks["id"], "name": "Drink voucher", "quantity": 3, "price_cents": 800}
    ]
    assert [i["seat_id"] for i in body["items"]] == seats[:1]

    # The same lines come back when reading the order, alone or in the list.
    headers = {"X-API-Key": user}
    assert (
        client.get(f"/orders/{body['id']}", headers=headers).json()["products"] == body["products"]
    )
    listed = client.get("/orders", headers=headers).json()["items"]
    assert next(o for o in listed if o["id"] == body["id"])["products"] == body["products"]


def test_merch_on_its_own(client: httpx.Client, user: str) -> None:
    poster = new_product(client, name="Poster", price_cents=1200)
    placed = order(client, user, products=[line(poster, 2)])
    assert placed.status_code == 201, placed.text
    assert placed.json()["items"] == [] and placed.json()["total_cents"] == 2400


def test_an_add_on_needs_a_seat_at_its_event(
    client: httpx.Client, users, new_event: NewEvent
) -> None:
    event_id, seats = new_event(seats=2)
    parking = new_product(client, event_id=event_id, name="Parking", price_cents=1500)
    buyer = users.key()

    refused = order(client, buyer, products=[line(parking)])
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "Add-ons need a seat at their event, in this order or one you've already bought"
    )
    # A seat at another event doesn't count.
    _, elsewhere = new_event(seats=1)
    assert order(client, buyer, seat_ids=elsewhere, products=[line(parking)]).status_code == 409

    # With a seat in an earlier order, parking can be bought later.
    assert order(client, buyer, seat_ids=seats[:1]).status_code == 201
    assert order(client, buyer, products=[line(parking)]).status_code == 201


def test_stock_runs_down_and_runs_out(client: httpx.Client, users) -> None:
    shirt = new_product(client, name="Last shirts", stock=3, max_per_order=4)
    first = users.key()
    assert order(client, first, products=[line(shirt, 2)]).status_code == 201
    assert (
        next(p for p in client.get("/products").json() if p["id"] == shirt["id"])["stock_left"] == 1
    )

    too_many = order(client, users.key(), products=[line(shirt, 2)])
    assert too_many.status_code == 409
    assert too_many.json()["detail"] == "Only 1 of Last shirts left"
    assert order(client, users.key(), products=[line(shirt, 1)]).status_code == 201
    gone = order(client, users.key(), products=[line(shirt, 1)])
    assert gone.status_code == 409 and gone.json()["detail"] == "Last shirts is sold out"


def test_a_failed_order_takes_nothing(client: httpx.Client, users, new_event: NewEvent) -> None:
    """Stock and seats are only taken when the whole order succeeds."""
    event_id, seats = new_event(seats=1)
    shirt = new_product(client, name="Rollback shirt", stock=5)
    rival, buyer = users.key(), users.key()
    assert order(client, rival, seat_ids=seats).status_code == 201

    failed = order(client, buyer, seat_ids=seats, products=[line(shirt, 2)])
    assert failed.status_code == 409
    assert (
        next(p for p in client.get("/products").json() if p["id"] == shirt["id"])["stock_left"] == 5
    )


@pytest.mark.parametrize(
    ("products", "status"),
    [
        pytest.param([{"product_id": 999_999, "quantity": 1}], 404, id="unknown product"),
        pytest.param("twice", 409, id="the same product twice"),
        pytest.param([{"product_id": 1, "quantity": 0}], 422, id="none of something"),
        pytest.param([{"product_id": 1, "quantity": -1}], 422, id="a negative quantity"),
        pytest.param([{"product_id": 1, "quantity": 11}], 422, id="more than 10 of something"),
        pytest.param(
            [{"product_id": 1, "quantity": 1, "price_cents": 1}], 422, id="a price of your own"
        ),
    ],
)
def test_bad_product_lines_are_refused(
    client: httpx.Client, user: str, products: Any, status: int
) -> None:
    if products == "twice":
        poster = new_product(client, name="Twice")
        products = [line(poster), line(poster)]
    assert order(client, user, products=products).status_code == status


def test_the_per_order_limit(client: httpx.Client, user: str) -> None:
    item = new_product(client, name="Limited", max_per_order=2)
    over = order(client, user, products=[line(item, 3)])
    assert over.status_code == 409
    assert over.json()["detail"] == "You can buy at most 2 of Limited in one order"


def test_an_empty_order_is_refused(client: httpx.Client, user: str) -> None:
    assert order(client, user).status_code == 422
    assert order(client, user, seat_ids=[], products=[]).status_code == 422


# -------------------------------------------------------------- promo codes


def test_a_percentage_off_rounds_down_to_the_cent(client: httpx.Client, user: str) -> None:
    item = new_product(client, name="Odd price", price_cents=3333)
    code = new_promo(client, percent_off=10)
    placed = order(client, user, products=[line(item)], promo_code=code)
    assert placed.status_code == 201, placed.text
    # 10% of $33.33 is $3.333: the buyer gets $3.33, never more than promised.
    assert placed.json()["discount_cents"] == 333
    assert placed.json()["total_cents"] == 3000
    assert placed.json()["promo_code"] == code


def test_codes_apply_to_what_they_say(client: httpx.Client, users, new_event: NewEvent) -> None:
    event_id, seats = new_event(seats=2, price_cents=5000)
    drinks = new_product(client, event_id=event_id, name="Drinks", price_cents=1000)
    seats_only = new_promo(client, percent_off=20, applies_to="seats")
    products_only = new_promo(client, amount_off_cents=5000, applies_to="products")

    one = order(
        client, users.key(), seat_ids=seats[:1], products=[line(drinks)], promo_code=seats_only
    )
    assert one.json()["discount_cents"] == 1000  # 20% of the $50 seat, not the drinks
    # A fixed amount never takes off more than what it applies to.
    two = order(
        client, users.key(), seat_ids=seats[1:], products=[line(drinks)], promo_code=products_only
    )
    assert two.json()["discount_cents"] == 1000
    assert two.json()["total_cents"] == 5000

    # A seats-only code on an order of merch has nothing to apply to.
    shirt = new_product(client, name="Shirt")
    refused = order(client, users.key(), products=[line(shirt)], promo_code=seats_only)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "That promo code only applies to seats"


@pytest.mark.parametrize(
    ("fields", "detail"),
    [
        ({"percent_off": 10, "expires_at": "2026-01-01T00:00:00Z"}, "That promo code has expired"),
        (
            {"percent_off": 10, "min_total_cents": 5000},
            "That promo code needs an order of at least $50.00",
        ),
        ({"percent_off": 10, "max_uses": 1}, "That promo code has been used up"),
    ],
    ids=["expired", "minimum order", "used up"],
)
def test_codes_that_cant_be_used_say_why(
    client: httpx.Client, users, fields: dict[str, Any], detail: str
) -> None:
    item = new_product(client, name="Promo test", price_cents=1000)
    code = new_promo(client, **fields)
    if "max_uses" in fields:
        assert order(client, users.key(), products=[line(item)], promo_code=code).status_code == 201
    refused = order(client, users.key(), products=[line(item)], promo_code=code)
    assert refused.status_code == 409
    assert refused.json()["detail"] == detail


def test_codes_are_case_insensitive_and_unknown_ones_are_refused(
    client: httpx.Client, users
) -> None:
    item = new_product(client, name="Case test", price_cents=1000)
    placed = order(client, users.key(), products=[line(item)], promo_code="welcome10")
    assert placed.status_code == 201 and placed.json()["promo_code"] == "WELCOME10"
    unknown = order(client, users.key(), products=[line(item)], promo_code="NOPE-123")
    assert unknown.status_code == 409 and unknown.json()["detail"] == "That promo code isn't valid"


def test_a_quote_matches_the_order_and_changes_nothing(
    client: httpx.Client, user: str, new_event: NewEvent
) -> None:
    event_id, seats = new_event(seats=2, price_cents=4500)
    drinks = new_product(client, event_id=event_id, name="Quote drinks", price_cents=800, stock=10)
    code = new_promo(client, percent_off=15, max_uses=1)
    basket = {"seat_ids": seats, "products": [line(drinks, 2)], "promo_code": code}

    quoted = quote(client, user, **basket)
    assert quoted.status_code == 200, quoted.text
    assert quoted.json() == {
        "seats_cents": 9000,
        "products_cents": 1600,
        "subtotal_cents": 10600,
        "discount_cents": 1590,
        "total_cents": 9010,
        "promo_code": code,
    }
    # Quoting twice uses nothing up.
    assert quote(client, user, **basket).status_code == 200
    placed = order(client, user, **basket)
    assert placed.json()["total_cents"] == quoted.json()["total_cents"]
    assert placed.json()["discount_cents"] == quoted.json()["discount_cents"]
    # Now the code's one use is gone, and the quote says so.
    assert quote(client, user, products=[line(drinks)], promo_code=code).status_code == 409


# ------------------------------------------------------------------ races


@pytest.mark.catches_oversell
def test_thirty_buyers_one_shirt_left_exactly_one_gets_it(
    base_url: str, client: httpx.Client
) -> None:
    shirt = new_product(client, name="The last shirt", stock=1)
    keys = [user_key(n) for n in range(100, 130)]
    start = threading.Barrier(len(keys))
    with httpx.Client(base_url=base_url, timeout=60) as shared:

        def buy(key: str) -> int:
            start.wait()
            return order(shared, key, products=[line(shirt)]).status_code

        with ThreadPoolExecutor(max_workers=len(keys)) as pool:
            statuses = list(pool.map(buy, keys))

    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == len(keys) - 1
    assert (
        next(p for p in client.get("/products").json() if p["id"] == shirt["id"])["stock_left"] == 0
    )


@pytest.mark.catches_promo_race
def test_a_single_use_code_is_used_once_however_many_try_at_once(
    base_url: str, client: httpx.Client
) -> None:
    code = new_promo(client, percent_off=50, max_uses=1)
    keys = [user_key(n) for n in range(130, 150)]
    # A different product for each buyer, so nothing but the code is shared:
    # a shared product's row lock would line the orders up by itself.
    items = {key: new_product(client, name=f"Race item {n}") for n, key in enumerate(keys)}
    start = threading.Barrier(len(keys))
    with httpx.Client(base_url=base_url, timeout=60) as shared:

        def buy(key: str) -> int:
            start.wait()
            return order(shared, key, products=[line(items[key])], promo_code=code).status_code

        with ThreadPoolExecutor(max_workers=len(keys)) as pool:
            statuses = list(pool.map(buy, keys))

    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == len(keys) - 1


# ------------------------------------------------------------------- admin


def test_only_admins_add_products_and_codes(client: httpx.Client, user: str) -> None:
    headers = {"X-API-Key": user}
    product = {"name": "x", "category": "merch", "price_cents": 1}
    assert client.post("/admin/products", headers=headers, json=product).status_code == 403
    promo = {"code": "MINE-100", "percent_off": 100}
    assert client.post("/admin/promo-codes", headers=headers, json=promo).status_code == 403
    # A code takes a percentage or an amount, not both.
    both = {"code": "BOTH-1", "percent_off": 10, "amount_off_cents": 100}
    assert client.post("/admin/promo-codes", headers=admin(), json=both).status_code == 422
    code = new_promo(client, percent_off=5)
    again = client.post(
        "/admin/promo-codes", headers=admin(), json={"code": code, "percent_off": 5}
    )
    assert again.status_code == 409

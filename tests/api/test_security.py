"""Checks from the OWASP API Security Top 10 (2023)."""

from __future__ import annotations

import httpx
import pytest

from tests.api.conftest import metric


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/orders"), ("GET", "/orders/1"), ("POST", "/orders"), ("POST", "/admin/events")],
)
def test_api2_authenticated_endpoints_need_a_valid_key(
    client: httpx.Client, method: str, path: str
) -> None:
    body = {"seat_ids": [1]} if path == "/orders" else None
    missing = client.request(method, path, json=body)
    wrong = client.request(method, path, json=body, headers={"X-API-Key": "key-nobody"})
    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_api1_cannot_read_someone_elses_order(client: httpx.Client, new_event, users) -> None:
    _, seats = new_event(seats=1)
    owner, other = users.key(), users.key()
    order = client.post("/orders", headers={"X-API-Key": owner}, json={"seat_ids": seats}).json()

    peek = client.get(f"/orders/{order['id']}", headers={"X-API-Key": other})
    assert peek.status_code == 404  # not 403: don't confirm the order exists

    their_list = client.get("/orders", headers={"X-API-Key": other}).json()
    assert order["id"] not in [o["id"] for o in their_list["items"]]


@pytest.mark.parametrize(
    "extra", [{"user_id": 1}, {"total_cents": 0}, {"status": "paid"}, {"is_admin": True}]
)
def test_api3_mass_assignment_is_rejected(
    client: httpx.Client, new_event, user, extra: dict[str, object]
) -> None:
    _, seats = new_event(seats=1)
    response = client.post(
        "/orders", headers={"X-API-Key": user}, json={"seat_ids": seats, **extra}
    )
    assert response.status_code == 422


def test_api3_responses_do_not_leak_keys_or_users(client: httpx.Client, new_event, user) -> None:
    _, seats = new_event(seats=1)
    order = client.post("/orders", headers={"X-API-Key": user}, json={"seat_ids": seats})
    text = order.text.lower()
    assert "api_key" not in text and "key-user" not in text and "user_id" not in text


def test_api4_rate_limit_returns_429_with_retry_after(client: httpx.Client, user) -> None:
    before = metric(client, "rate_limited_total")
    statuses = [client.get("/orders", headers={"X-API-Key": user}).status_code for _ in range(130)]
    assert statuses[0] == 200
    assert 429 in statuses
    limited = client.get("/orders", headers={"X-API-Key": user})
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert metric(client, "rate_limited_total") > before


def test_api4_rate_limit_is_per_key(client: httpx.Client, users) -> None:
    noisy, quiet = users.key(), users.key()
    for _ in range(130):
        client.get("/orders", headers={"X-API-Key": noisy})
    assert client.get("/orders", headers={"X-API-Key": quiet}).status_code == 200


def test_api5_normal_users_cannot_use_admin_endpoints(client: httpx.Client, user) -> None:
    response = client.post(
        "/admin/events",
        headers={"X-API-Key": user},
        json={
            "name": "x",
            "venue": "y",
            "starts_at": "2027-01-01T00:00:00Z",
            "seats": 1,
            "price_cents": 1,
        },
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "path", ["/events/0", "/events/-1", "/events/99999999999999", "/events/abc"]
)
def test_api8_bad_ids_are_client_errors_not_crashes(client: httpx.Client, path: str) -> None:
    assert client.get(path).status_code in (404, 422)


def test_api8_nul_bytes_are_rejected_not_crashing_the_database(
    client: httpx.Client, admin_headers: dict[str, str]
) -> None:
    response = client.post(
        "/admin/events",
        headers=admin_headers,
        json={
            "name": "bad\u0000name",
            "venue": "v",
            "starts_at": "2027-01-01T00:00:00Z",
            "seats": 1,
            "price_cents": 1,
        },
    )
    assert response.status_code == 422

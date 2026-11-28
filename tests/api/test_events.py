"""Functional tests for the read endpoints."""

from __future__ import annotations

import httpx

from tests.api.conftest import SEED_EVENTS, SEED_SEATS


def test_health_reports_which_problems_are_on(client: httpx.Client) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert isinstance(body["bugs"], list)


def test_events_are_paged_in_id_order(client: httpx.Client) -> None:
    first = client.get("/events", params={"limit": 5}).json()
    second = client.get("/events", params={"limit": 5, "offset": 5}).json()

    first_ids = [e["id"] for e in first["items"]]
    second_ids = [e["id"] for e in second["items"]]
    assert first_ids == sorted(first_ids)
    assert len(first_ids) == 5 and len(second_ids) == 5
    assert max(first_ids) < min(second_ids)
    assert first["total"] >= SEED_EVENTS


def test_event_list_counts_available_seats(client: httpx.Client) -> None:
    page = client.get("/events", params={"limit": 3}).json()
    for event in page["items"]:
        single = client.get(f"/events/{event['id']}").json()
        assert event["available_seats"] == single["available_seats"]


def test_seeded_event_has_all_seats(client: httpx.Client) -> None:
    event = client.get(f"/events/{SEED_EVENTS}").json()
    assert event["available_seats"] == SEED_SEATS


def test_unknown_event_is_404(client: httpx.Client) -> None:
    for path in ("/events/999999", "/events/999999/seats", "/events/999999/stats"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json() == {"detail": "Event not found"}


def test_seat_list_filters_by_status(client: httpx.Client, new_event, user) -> None:
    event_id, seats = new_event(seats=4)
    bought = client.post("/orders", headers={"X-API-Key": user}, json={"seat_ids": seats[:1]})
    assert bought.status_code == 201

    sold = client.get(f"/events/{event_id}/seats", params={"status": "sold"}).json()
    available = client.get(f"/events/{event_id}/seats").json()
    assert [s["id"] for s in sold["items"]] == seats[:1]
    assert [s["id"] for s in available["items"]] == seats[1:]


def test_invalid_seat_status_is_rejected(client: httpx.Client) -> None:
    assert client.get("/events/1/seats", params={"status": "reserved"}).status_code == 422


def test_stats_add_up_sales(client: httpx.Client, new_event, user) -> None:
    event_id, seats = new_event(seats=3, price_cents=1500)
    client.post("/orders", headers={"X-API-Key": user}, json={"seat_ids": seats[:2]})

    stats = client.get(f"/events/{event_id}/stats").json()
    assert stats == {
        "event_id": event_id,
        "seats_total": 3,
        "seats_sold": 2,
        "revenue_cents": 3000,
    }


def test_every_response_reports_its_query_count(client: httpx.Client) -> None:
    response = client.get("/events/1")
    assert int(response.headers["X-DB-Queries"]) >= 1

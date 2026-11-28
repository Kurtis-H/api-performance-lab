"""Venue layouts: events made of sections, each a price tier of lettered rows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, text

from app.config import Settings
from app.db import _add_seat_layout, make_engine

STARTS = datetime(2027, 9, 1, 19, tzinfo=UTC).isoformat()

THEATER = [
    {"name": "Orchestra", "rows": 2, "seats_per_row": 3, "price_cents": 9000},
    {"name": "Balcony", "rows": 1, "seats_per_row": 2, "price_cents": 4000},
]


def create(client: httpx.Client, headers: dict[str, str], **body: Any) -> httpx.Response:
    return client.post(
        "/admin/events",
        headers=headers,
        json={"name": "Layout test", "venue": "Test hall", "starts_at": STARTS, **body},
    )


def all_seats(client: httpx.Client, event_id: int) -> list[dict[str, Any]]:
    page = client.get(f"/events/{event_id}/seats", params={"limit": 100})
    assert page.status_code == 200, page.text
    return page.json()["items"]


def test_sections_become_lettered_rows_at_their_own_price(
    client: httpx.Client, admin_headers: dict[str, str]
) -> None:
    created = create(client, admin_headers, sections=THEATER)
    assert created.status_code == 201, created.text
    assert created.json()["seats"] == 8

    seats = all_seats(client, created.json()["id"])
    where = [(s["section"], s["row"], s["number"], s["label"], s["price_cents"]) for s in seats]
    assert where == [
        ("Orchestra", "A", 1, "A-1", 9000),
        ("Orchestra", "A", 2, "A-2", 9000),
        ("Orchestra", "A", 3, "A-3", 9000),
        ("Orchestra", "B", 1, "B-1", 9000),
        ("Orchestra", "B", 2, "B-2", 9000),
        ("Orchestra", "B", 3, "B-3", 9000),
        # Rows run on across sections, so labels stay unique within the event.
        ("Balcony", "C", 1, "C-1", 4000),
        ("Balcony", "C", 2, "C-2", 4000),
    ]


def test_rows_skip_i_and_o(client: httpx.Client, admin_headers: dict[str, str]) -> None:
    sections = [
        {"name": f"Tier {n}", "rows": 6, "seats_per_row": 1, "price_cents": 1000} for n in range(4)
    ]
    created = create(client, admin_headers, sections=sections)
    assert created.status_code == 201, created.text
    rows = [s["row"] for s in all_seats(client, created.json()["id"])]
    assert len(rows) == 24
    assert "I" not in rows and "O" not in rows
    assert rows[-1] == "Z"


def test_simple_events_are_one_general_row(
    client: httpx.Client, admin_headers: dict[str, str]
) -> None:
    created = create(client, admin_headers, seats=3, price_cents=2500)
    assert created.status_code == 201, created.text
    seats = all_seats(client, created.json()["id"])
    assert [(s["section"], s["row"], s["number"], s["label"]) for s in seats] == [
        ("General", "S", 1, "S-1"),
        ("General", "S", 2, "S-2"),
        ("General", "S", 3, "S-3"),
    ]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"seats": 3, "price_cents": 100, "sections": THEATER}, id="both shapes"),
        pytest.param({}, id="neither shape"),
        pytest.param({"seats": 3}, id="seats without a price"),
        pytest.param({"sections": []}, id="no sections"),
        pytest.param({"sections": THEATER * 3}, id="too many sections"),
        pytest.param(
            {"sections": [{**THEATER[0], "rows": 7}]},
            id="too many rows in a section",
        ),
        pytest.param(
            {"sections": [{**THEATER[0], "seats_per_row": 41}]},
            id="too many seats in a row",
        ),
        pytest.param(
            {"sections": [{**THEATER[0], "price_cents": -1}]},
            id="negative price",
        ),
        pytest.param(
            {"sections": [{**THEATER[0], "name": ""}]},
            id="unnamed section",
        ),
    ],
)
def test_bad_layouts_are_refused(
    client: httpx.Client, admin_headers: dict[str, str], body: dict[str, Any]
) -> None:
    assert create(client, admin_headers, **body).status_code == 422


def test_the_biggest_layout_the_spec_allows_fits(
    client: httpx.Client, admin_headers: dict[str, str]
) -> None:
    sections = [
        {"name": f"Tier {n}", "rows": 6, "seats_per_row": 40, "price_cents": 1000} for n in range(4)
    ]
    created = create(client, admin_headers, sections=sections)
    assert created.status_code == 201, created.text
    assert created.json()["seats"] == 960


def test_an_old_database_gets_section_row_and_number(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS layout_migration CASCADE"))
        conn.execute(text("CREATE SCHEMA layout_migration"))
    migrating = create_engine(
        settings.database_url, connect_args={"options": "-c search_path=layout_migration"}
    )
    try:
        with migrating.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE seats (id serial PRIMARY KEY, event_id int, "
                    "label varchar(20), price_cents int, status varchar(12))"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO seats (event_id, label, price_cents, status) VALUES "
                    "(1, 'R3-12', 3500, 'available'), (1, 'S-7', 4000, 'sold')"
                )
            )
        _add_seat_layout(migrating)
        _add_seat_layout(migrating)  # running it again is harmless
        with migrating.connect() as conn:
            rows = conn.execute(
                text("SELECT label, section, seat_row, seat_number FROM seats ORDER BY id")
            ).all()
        assert [tuple(r) for r in rows] == [
            ("R3-12", "General", "R3", 12),
            ("S-7", "General", "S", 7),
        ]
    finally:
        migrating.dispose()
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA layout_migration CASCADE"))
        engine.dispose()

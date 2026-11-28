"""Provider side of the consumer contracts: replay every consumer's pact here.

booking-web and booking-mobile each record what they need from this API as a
Pact file (`pacts/*.json` in their repos). CI checks those repos out into
`consumers/`, and this test replays every interaction against the real API.
If a change here would break one of the apps, this test fails on the API's
own pull request, before anything is deployed.

Provider states ("an event with available seats exists") are set up by a small
HTTP endpoint that creates the data through the public and admin API and
returns the real ids, which Pact puts into the request (`fromProviderState`).
So no state depends on fixed ids, and nothing resets the shared database.

Set PACT_DIR to point somewhere else, and REQUIRE_PACTS=1 to fail rather than
skip when no pact files are found (CI does).
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

pact = pytest.importorskip("pact", reason="pact-python is a dev dependency")

ADMIN = {"X-API-Key": "key-admin"}
# The consumers' contracts use these keys; 97 is a rival buyer for the sold-seat state.
BUYER, RIVAL = "key-user-98", "key-user-97"

PACT_DIR = Path(os.environ.get("PACT_DIR", Path(__file__).parents[2] / "consumers"))
PACT_FILES = sorted(PACT_DIR.glob("**/pacts/*.json"))


def _new_event(client: httpx.Client, seats: int) -> tuple[int, list[int]]:
    created = client.post(
        "/admin/events",
        headers=ADMIN,
        json={
            "name": "Contract test",
            "venue": "Pact hall",
            "starts_at": datetime(2027, 6, 1, 19, tzinfo=UTC).isoformat(),
            "seats": seats,
            "price_cents": 3500,
        },
    )
    created.raise_for_status()
    event_id = created.json()["id"]
    page = client.get(f"/events/{event_id}/seats", params={"limit": 100})
    page.raise_for_status()
    return event_id, [s["id"] for s in page.json()["items"]]


def _buy(client: httpx.Client, key: str, seat_id: int) -> int:
    placed = client.post("/orders", headers={"X-API-Key": key}, json={"seat_ids": [seat_id]})
    placed.raise_for_status()
    return int(placed.json()["id"])


def states(client: httpx.Client) -> dict[str, Callable[[], dict[str, int]]]:
    """Each consumer state: create its data, return the ids the pact asks for."""

    def available() -> dict[str, int]:
        event_id, seat_ids = _new_event(client, 4)
        return {"eventId": event_id, "seatId": seat_ids[0]}

    def sold() -> dict[str, int]:
        event_id, seat_ids = _new_event(client, 2)
        _buy(client, RIVAL, seat_ids[0])
        return {"eventId": event_id, "seatId": seat_ids[0]}

    def holds_seats() -> dict[str, int]:
        # The buyer holds the first seat; a rival holds the second.
        event_id, seat_ids = _new_event(client, 4)
        for key, seat_id in [(BUYER, seat_ids[0]), (RIVAL, seat_ids[1])]:
            client.put(
                f"/events/{event_id}/hold", headers={"X-API-Key": key}, json={"seat_ids": [seat_id]}
            ).raise_for_status()
        return {"eventId": event_id, "seatId": seat_ids[0], "rivalSeatId": seat_ids[1]}

    def shop_open() -> dict[str, int]:
        # An event with seats and an add-on, and a merch item.
        event_id, seat_ids = _new_event(client, 4)
        add_on = client.post(
            "/admin/products",
            headers=ADMIN,
            json={
                "event_id": event_id,
                "name": "Parking",
                "category": "parking",
                "price_cents": 1500,
                "stock": 10,
            },
        )
        add_on.raise_for_status()
        merch = client.post(
            "/admin/products",
            headers=ADMIN,
            json={"name": "Poster", "category": "merch", "price_cents": 1200, "stock": 10},
        )
        merch.raise_for_status()
        return {
            "eventId": event_id,
            "seatId": seat_ids[0],
            "productId": add_on.json()["id"],
            "merchId": merch.json()["id"],
        }

    def has_order() -> dict[str, int]:
        _, seat_ids = _new_event(client, 2)
        return {"orderId": _buy(client, BUYER, seat_ids[0])}

    return {
        "events exist": lambda: {},  # the seed data has them
        "an event with available seats exists": available,
        "a seat has already been sold": sold,
        "the buyer has an order": has_order,
        "the buyer holds a seat": holds_seats,
        # The seed data has the demo promo codes, such as WELCOME10.
        "the shop has products": shop_open,
    }


@pytest.fixture(scope="module")
def state_url(client: httpx.Client) -> Iterator[str]:
    handlers = states(client)
    unknown: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - the stdlib's name
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body: dict[str, int] = {}
            if data.get("action", "setup") == "setup" and data.get("state"):
                handler = handlers.get(data["state"])
                if handler is None:
                    unknown.append(data["state"])
                    self.send_error(500, f"unknown provider state {data['state']!r}")
                    return
                body = handler()
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/state"
    server.shutdown()
    assert not unknown, f"consumers asked for states this API doesn't set up: {unknown}"


def test_pact_files_are_present() -> None:
    if not PACT_FILES:
        if os.environ.get("REQUIRE_PACTS"):
            pytest.fail(f"no pact files under {PACT_DIR}")
        pytest.skip(f"no pact files under {PACT_DIR} (CI checks out the consumers there)")


@pytest.mark.parametrize("pact_file", PACT_FILES, ids=lambda p: p.stem)
def test_api_honours_consumer_contract(pact_file: Path, base_url: str, state_url: str) -> None:
    (
        pact.Verifier("booking-api", host="127.0.0.1")
        .add_transport(url=base_url)
        .add_source(pact_file)
        .state_handler(state_url, body=True)
        .verify()
    )

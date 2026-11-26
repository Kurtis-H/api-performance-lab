// Race: 50 buyers hit the same last seat at the same moment.
// Exactly one may get it. Anything else is overselling.
import http from "k6/http";
import { Counter } from "k6/metrics";
import { BASE_URL } from "./lib/traffic.js";

const BUYERS = Number(__ENV.BUYERS || 50);
const won = new Counter("orders_won");
const lost = new Counter("orders_lost");

export const options = {
  scenarios: {
    last_seat: {
      executor: "per-vu-iterations",
      vus: BUYERS,
      iterations: 1,
    },
  },
  thresholds: {
    orders_won: ["count==1"],
    orders_lost: [`count==${BUYERS - 1}`],
  },
};

export function setup() {
  const headers = { "Content-Type": "application/json", "X-API-Key": "key-admin" };
  const event = http.post(
    `${BASE_URL}/admin/events`,
    JSON.stringify({
      name: "Last seat",
      venue: "k6 arena",
      starts_at: "2027-05-01T19:00:00Z",
      seats: 1,
      price_cents: 9900,
    }),
    { headers },
  ).json();
  const seat = http.get(`${BASE_URL}/events/${event.id}/seats`).json("items")[0];
  // Line everyone up a moment in the future so they all fire together.
  return { seatId: seat.id, fireAt: Date.now() + 3000 };
}

export default function ({ seatId, fireAt }) {
  while (Date.now() < fireAt) {
    // busy-wait for the start line; sleep() isn't precise enough here
  }
  const res = http.post(`${BASE_URL}/orders`, JSON.stringify({ seat_ids: [seatId] }), {
    headers: { "Content-Type": "application/json", "X-API-Key": `key-user-${__VU}` },
    responseCallback: http.expectedStatuses(201, 409),
  });
  if (res.status === 201) won.add(1);
  else if (res.status === 409) lost.add(1);
}

export function handleSummary(data) {
  const w = data.metrics.orders_won ? data.metrics.orders_won.values.count : 0;
  const l = data.metrics.orders_lost ? data.metrics.orders_lost.values.count : 0;
  return { stdout: `\nrace: ${w} buyer(s) got the seat, ${l} were turned away (want 1 and ${BUYERS - 1})\n` };
}

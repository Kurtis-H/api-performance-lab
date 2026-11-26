// Hot event: a flash crowd all looking at one event's stats at once
// (a show sells out and everyone refreshes). With a 5 s cache, the report
// should be computed about once every 5 s, however many people are waiting.
import http from "k6/http";
import { check } from "k6";
import { Gauge } from "k6/metrics";
import { BASE_URL, summary } from "./lib/traffic.js";

const DURATION_S = Number((__ENV.DURATION || "40s").replace("s", ""));
const TTL_S = Number(__ENV.STATS_TTL_SECONDS || 5);
// One recompute per TTL window, plus a little slack for window edges.
const RECOMPUTE_BUDGET = Math.ceil(DURATION_S / TTL_S) + 2;
const recomputes = new Gauge("stats_recomputes");

function recomputedSoFar() {
  const line = http
    .get(`${BASE_URL}/metrics`)
    .body.split("\n")
    .find((l) => l.startsWith("stats_recomputes_total "));
  return Number(line.split(" ")[1]);
}

export const options = {
  scenarios: {
    flash_crowd: {
      executor: "constant-vus",
      vus: Number(__ENV.VUS || 100),
      duration: `${DURATION_S}s`,
    },
  },
  thresholds: {
    "http_req_duration{name:GET /events/{id}/stats}": ["p(95)<500"],
    http_req_failed: ["rate<0.01"],
    stats_recomputes: [`value<=${RECOMPUTE_BUDGET}`],
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/events/1/stats`, {
    tags: { name: "GET /events/{id}/stats" },
  });
  check(res, { "stats are 200": (r) => r.status === 200 });
}

export function setup() {
  return { before: recomputedSoFar() };
}

export function teardown({ before }) {
  const count = recomputedSoFar() - before;
  recomputes.add(count);
  console.log(`stats recomputed ${count} times (budget ${RECOMPUTE_BUDGET})`);
}

export const handleSummary = summary("hot_event");

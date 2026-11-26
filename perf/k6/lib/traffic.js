// Shared traffic mix and helpers for every k6 scenario.
//
// The mix is roughly what a ticketing site sees:
//   60% browse (event list, one event, its seats)
//   25% look at stats for a handful of popular events
//   15% try to buy a seat (a 409 for a just-sold seat is a normal outcome)

import http from "k6/http";
import { check, group, sleep } from "k6";
import { Counter, Gauge, Trend } from "k6/metrics";

export const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
export const EVENTS = Number(__ENV.EVENTS || 200);
export const USERS = Number(__ENV.USERS || 200);
const HOT_EVENTS = 5;
const THINK_TIME = Number(__ENV.THINK_TIME || 0.5);

// Endpoint names used as the `name` tag, so URLs with ids group together.
export const ENDPOINTS = [
  "GET /events",
  "GET /events/{id}",
  "GET /events/{id}/seats",
  "GET /events/{id}/stats",
  "POST /orders",
];

export const dbQueries = new Trend("db_queries", false);
export const seatsSold = new Counter("seats_sold");
export const seatConflicts = new Counter("seat_conflicts");
export const rssGrowthMb = new Gauge("rss_growth_mb");

const json = { "Content-Type": "application/json" };

function apiKey() {
  return `key-user-${(__VU % USERS) + 1}`;
}

function randomInt(max) {
  return Math.floor(Math.random() * max) + 1;
}

function get(path, name) {
  const res = http.get(`${BASE_URL}${path}`, { tags: { name } });
  recordQueries(res, name);
  return res;
}

function recordQueries(res, name) {
  const queries = res.headers["X-Db-Queries"];
  if (queries !== undefined) dbQueries.add(Number(queries), { name });
}

function browse() {
  group("browse", () => {
    const offset = randomInt(Math.max(1, EVENTS - 20)) - 1;
    const list = get(`/events?limit=20&offset=${offset}`, "GET /events");
    check(list, { "event list is 200": (r) => r.status === 200 });

    const id = randomInt(EVENTS);
    const one = get(`/events/${id}`, "GET /events/{id}");
    check(one, { "event is 200": (r) => r.status === 200 });

    // No limit on purpose: like many front ends, this relies on the server's
    // default page size. That is exactly the client no_page_limit hurts.
    const seats = get(`/events/${id}/seats`, "GET /events/{id}/seats");
    check(seats, { "seat map is 200": (r) => r.status === 200 });
  });
}

function stats() {
  group("stats", () => {
    const res = get(`/events/${randomInt(HOT_EVENTS)}/stats`, "GET /events/{id}/stats");
    check(res, { "stats are 200": (r) => r.status === 200 });
  });
}

function buy() {
  group("buy", () => {
    const id = randomInt(EVENTS);
    const offset = randomInt(40) - 1;
    const seats = get(`/events/${id}/seats?limit=1&offset=${offset}`, "GET /events/{id}/seats");
    const items = seats.status === 200 ? seats.json("items") : [];
    if (!items || items.length === 0) return;

    const res = http.post(
      `${BASE_URL}/orders`,
      JSON.stringify({ seat_ids: [items[0].id] }),
      {
        headers: { ...json, "X-API-Key": apiKey() },
        tags: { name: "POST /orders" },
        // Losing a seat to another buyer is expected, not an error.
        responseCallback: http.expectedStatuses(201, 409),
      },
    );
    recordQueries(res, "POST /orders");
    if (res.status === 201) seatsSold.add(1);
    if (res.status === 409) seatConflicts.add(1);
    check(res, { "order is 201 or 409": (r) => r.status === 201 || r.status === 409 });
  });
}

// think=false skips the pause between journeys (used for warm-up).
export function userJourney(think = true) {
  const roll = Math.random();
  if (roll < 0.6) browse();
  else if (roll < 0.85) stats();
  else buy();
  if (think) sleep(THINK_TIME * Math.random() * 2);
}

// ------------------------------------------------------------ memory checks

export function residentMemoryMb() {
  const res = http.get(`${BASE_URL}/metrics`, { tags: { name: "metrics" } });
  const line = res.body
    .split("\n")
    .find((l) => l.startsWith("process_resident_memory_bytes "));
  return line ? Number(line.split(" ")[1]) / 1024 / 1024 : NaN;
}

// --------------------------------------------------------------- thresholds

// p95 latency budget (ms) and query budget for each endpoint.
const BUDGETS = {
  "GET /events": { p95: 300, queries: 3 },
  "GET /events/{id}": { p95: 200, queries: 3 },
  "GET /events/{id}/seats": { p95: 200, queries: 3 },
  "GET /events/{id}/stats": { p95: 500, queries: 4 },
  "POST /orders": { p95: 500, queries: 8 },
};

// latencyScale loosens the latency budget for scenarios that push past normal
// load on purpose (stress, spike). Query budgets never loosen: a page that
// needs more queries under load is a bug at any load.
// latency=false leaves latency out of pass/fail (stress tests report it instead).
export function thresholds({ latencyScale = 1, errorRate = 0.01, latency = true } = {}) {
  const t = {
    http_req_failed: [`rate<${errorRate}`],
    checks: [`rate>${1 - errorRate}`],
  };
  for (const name of ENDPOINTS) {
    const budget = BUDGETS[name];
    t[`http_req_duration{name:${name}}`] = latency
      ? [`p(95)<${budget.p95 * latencyScale}`]
      : ["p(95)>=0"]; // still listed, so the summary table shows it
    t[`db_queries{name:${name}}`] = [`max<=${budget.queries}`];
  }
  return t;
}

// -------------------------------------------------------------- summaries

function fmt(value) {
  return value === undefined ? "-" : Number(value).toFixed(1);
}

// Writes the full k6 data to results/<scenario>.json (used by the regression
// gate) and prints a short per-endpoint table.
export function summary(scenario) {
  return (data) => {
    const lines = [`\n${scenario}: per-endpoint results`, ""];
    lines.push(
      "endpoint                  reqs     p50 ms   p95 ms   max ms   queries(max)  threshold",
    );
    for (const name of ENDPOINTS) {
      const d = data.metrics[`http_req_duration{name:${name}}`];
      const q = data.metrics[`db_queries{name:${name}}`];
      const reqs = data.metrics[`http_reqs{name:${name}}`];
      if (!d) continue;
      const ok =
        Object.values(d.thresholds || {}).every((t) => t.ok) &&
        Object.values((q && q.thresholds) || {}).every((t) => t.ok);
      lines.push(
        [
          name.padEnd(25),
          String(reqs ? reqs.values.count : "-").padEnd(8),
          fmt(d.values.med).padEnd(8),
          fmt(d.values["p(95)"]).padEnd(8),
          fmt(d.values.max).padEnd(8),
          fmt(q && q.values.max).padEnd(13),
          ok ? "ok" : "FAILED",
        ].join(" "),
      );
    }
    const failed = data.metrics.http_req_failed;
    if (failed) lines.push("", `error rate: ${(failed.values.rate * 100).toFixed(2)}%`);
    if (data.metrics.rss_growth_mb) {
      lines.push(`memory growth: ${fmt(data.metrics.rss_growth_mb.values.value)} MB/min`);
    }
    return {
      stdout: lines.join("\n") + "\n",
      [`results/${scenario}.json`]: JSON.stringify(data, null, 2),
    };
  };
}

// Every endpoint gets a request-count submetric so the summary can show it.
export function withCounts(t) {
  for (const name of ENDPOINTS) t[`http_reqs{name:${name}}`] = ["count>=0"];
  return t;
}

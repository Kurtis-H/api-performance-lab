# api-performance-lab

[![CI](https://github.com/Kurtis-H/api-performance-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/Kurtis-H/api-performance-lab/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Kurtis-H/api-performance-lab/actions/workflows/codeql.yml/badge.svg)](https://github.com/Kurtis-H/api-performance-lab/actions/workflows/codeql.yml)

A ticket-booking API with **six classic performance problems you can switch on**, and the API, contract, security, concurrency and load tests that catch each one, with measured before-and-after numbers.

Building it, the load tests also found **three real problems I hadn't planted** (a connection-pool deadlock, a cache that still used the database on every hit, and latency that depended on when autovacuum ran), and Schemathesis found **five contract bugs**. Those are written up in [docs/findings.md](docs/findings.md).

Everything here is free: Python, Postgres, k6, Prometheus, Grafana, Schemathesis and Newman, run on GitHub Actions' free runners.

## The six problems

Measured with k6 against 1,000 events and **1,000,000 seats**, 50 virtual users, unless noted.

| Problem | Switch on | What it does to the API | Caught by |
|---|---|---|---|
| [N+1 queries](docs/problems/n-plus-one.md) | `n_plus_one` | 22 queries per page instead of 2; `GET /events` p95 58 → 245 ms | query budget (pytest, k6, CI gate) |
| [Missing index](docs/problems/missing-index.md) | `missing_index` | `GET /events` p95 58 → **2,620 ms**; throughput -77% | index check (pytest), latency gate at realistic data size |
| [Memory leak](docs/problems/memory-leak.md) | `memory_leak` | grows 16.4 MB/min against 1.1 MB/min fixed; latency looks normal | soak test growth *rate*, buffer-size metric |
| [No page limit](docs/problems/no-page-limit.md) | `no_page_limit` | seat map p95 41 → 379 ms; slows every endpoint | pytest, Postman (OWASP API4), k6 budgets |
| [Double booking](docs/problems/race-condition.md) | `race` | 40 of 50 buyers get the same seat; normal load tests see **nothing** | 50-buyer contention test (pytest and k6) |
| [Cache stampede](docs/problems/cache-stampede.md) | `cache_stampede` | 310 report recomputes instead of 8; latency looks normal | recompute counter (pytest and k6 flash crowd) |

Three of the six don't show up in a normal load test at all. Each page explains which kind of test does catch it, and why.

## The tests

| Layer | Tool | What it checks |
|---|---|---|
| API | pytest + httpx against a live server | Behavior of every endpoint, including the unhappy paths |
| Security | pytest | OWASP API Top 10: broken object-level auth, auth, mass assignment, rate limits, admin-only functions, bad input |
| Contract and fuzzing | [Schemathesis](https://schemathesis.readthedocs.io/) | Every response matches the OpenAPI spec, for generated valid *and* invalid requests |
| Concurrency | pytest, 50 threads on a barrier | 50 buyers, 1 seat: exactly 1 succeeds |
| End to end | Postman collection run by Newman | A tester's walkthrough with 29 assertions |
| Consumer contracts | [Pact](https://docs.pact.io/) | Replays the contracts [booking-web](https://github.com/Kurtis-H/booking-web) and [booking-mobile](https://github.com/Kurtis-H/booking-mobile) record, so a change that would break either app fails here first |
| Load | k6 | Smoke, load, stress, spike, soak, race and flash-crowd scenarios with thresholds |
| Regression gate | `perf/compare.py` | Fails CI if queries per request rise at all, or p95 latency more than doubles |
| Self-check | `scripts/prove_problems.py` | Each problem's test passes when fixed **and fails when the problem is on** |
| Monitoring | Prometheus + Grafana | Dashboard with a panel for each problem; k6 streams its own metrics in |

Every response carries an `X-DB-Queries` header. Query counts don't change with machine speed, so they make a noise-free performance gate. N+1 shows up as a number, not a hunch.

## Run it

Needs Python 3.11+, Postgres 16 and [k6](https://grafana.com/docs/k6/latest/set-up/install-k6/).

```bash
pip install -e ".[dev]"
export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/booking

pytest                                   # everything except load tests (~15 s)
python scripts/prove_problems.py         # each test catches its problem

EVENTS=1000 SEATS=1000 scripts/serve.sh  # seed 1M seats and start the API
k6 run perf/k6/load.js                   # in another terminal

PERF_BUGS=missing_index scripts/serve.sh # same again with a problem on
k6 run perf/k6/load.js
python -m perf.compare results/load.json --baseline perf/baseline.json
```

Or the whole lab with dashboards, using Docker:

```bash
docker compose up -d                             # API, Postgres, Prometheus, Grafana
docker compose run --rm k6                       # load test, streaming into Prometheus
open http://localhost:3000                       # "Booking API" dashboard
PERF_BUGS=n_plus_one docker compose up -d api    # switch a problem on and watch
```

### Load scenarios

| Script | Shape | Passes when |
|---|---|---|
| `smoke.js` | 1 user, 30 s | Everything works and is inside budget |
| `load.js` | ramp to 50 users, hold 2 min | Per-endpoint p95 and query budgets hold; feeds the CI gate |
| `stress.js` | 50 → 300 users in steps | Errors < 2% and query budgets hold. Latency is reported, not judged: one worker bends at ~240 req/s (p95 ~1 s at 300 users, 0 errors) |
| `spike.js` | 10 → 300 users in 10 s and back | Nothing fails; reads stay under ~1.5 s p95 on one worker |
| `soak.js` | 30 users, 30 min (`SOAK_DURATION`) | Memory grows < 5 MB/min after warm-up |
| `race.js` | 50 users buy one seat at the same instant | Exactly 1 wins |
| `hot_event.js` | 100 users on one event's stats | Report computed once per cache window |

The traffic mix (`perf/k6/lib/traffic.js`) is 60% browsing, 25% stats on popular events and 15% buying. The seat-map request deliberately leaves `limit` off, like many real front ends do.

## CI

Every push and pull request runs:

* **test (3.11 / 3.12 / 3.13):** ruff, mypy, all pytest suites with 80% coverage required, pip-audit
* **Every problem is real and caught:** `prove_problems.py`
* **Consumer contracts (Pact):** checks out the apps' `pacts/`, replays every interaction against the real API, then runs `scripts/prove_contracts.py`: three breaking changes (a renamed field, 400 instead of 409, 403 instead of 404) must fail the contract, and a change to a field no app uses must not
* **Postman collection (Newman)**
* **Load tests and regression gate:** seeds 1,000,000 seats, runs smoke, load, race and flash-crowd scenarios, posts the gate table to the job summary, then **switches the missing index on and checks the gate fails**
* **Grafana and Prometheus stack:** starts the Docker stack, checks the API is scraped, the dashboard loads and k6 metrics arrive
* **CodeQL** on the Python, the k6 JavaScript and the workflows; Dependabot for pip, Actions and Docker

## Layout

```
app/            FastAPI app, models, cache, rate limiter, seed data
  main.py       search for settings.has( to see each problem next to its fix
tests/api/      live-server tests: functional, security, concurrency, contract, problems
tests/unit/     config, cache, rate limiter, regression gate
perf/k6/        k6 scenarios and the shared traffic mix
perf/compare.py the regression gate; perf/baseline.json is its baseline
postman/        Newman collection and environment
monitoring/     Prometheus config and the provisioned Grafana dashboard
docs/           one write-up per problem, plus findings.md
```

## Security

A penetration test of this API and booking-web is written up in [booking-pentest](https://github.com/Kurtis-H/booking-pentest), and these fixes came out of it:

| Finding | Fix | Setting |
|---|---|---|
| One account could buy every seat of an event (BP-02) | A buyer may hold at most 10 seats per event, across orders. The buyer's row is locked during the check, so parallel orders can't slip past it. This adds 2 queries to `POST /orders` (5 to 7, and the baseline says so) | `MAX_SEATS_PER_BUYER` (0 turns it off) |
| Failed sign-ins were never throttled (BP-03) | After 30 wrong keys in a minute from one address, every key from it gets `429` with `Retry-After`. It's checked before the lookup, so a right guess can't be told apart while blocked | `AUTH_FAILURES_PER_MINUTE` (0 turns it off) |
| Keys were stored in plain text (BP-06) | Only SHA-256 hashes are stored. An existing database is migrated in place at startup | |
| Authenticated responses could be cached (BP-08) | `Cache-Control: no-store` on every response to a request with a key | |

Accepted for the lab, with the reasons in the report: predictable seed keys (BP-01), no limit on anonymous reads (BP-04, which the load tests need), and the public `/metrics` and `X-DB-Queries` (BP-05, which the performance gates read).

## Notes

* API keys in the seed data (`key-admin`, `key-user-1` ...) are deliberately predictable test data. Only their hashes are stored.
* The payment provider and the heavy reporting query are stand-ins (`PAYMENT_DELAY_MS`, `STATS_DELAY_MS`) so the timing problems behave the way they do in real systems.
* Single-flight caching is per process. With several workers you'd want it in a shared cache (Redis, for example).

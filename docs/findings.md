# What the tests found that I didn't plant

The six problems in `PERF_BUGS` were put there on purpose. These weren't. The test suite found them while I was building the lab, in the *fixed* version of the API, which is the point of having these tests.

## 1. Connection pool deadlock under a traffic spike

**Found by:** `perf/k6/spike.js` (10 → 300 users in 10 s)

| | Before | After |
|---|---:|---:|
| Requests failing | 8.5% | 0% |
| Pool timeout errors in the log | 140 | 0 |
| Slowest request | 60 s (client timeout) | 1.2 s |

**Cause.** FastAPI runs sync endpoints, and the clean-up of sync dependencies, as separate jobs on a shared pool of 40 threads. The database session was opened by a dependency and only closed in that dependency's clean-up. A request that had finished its work kept its database connection while it queued for a thread to run the clean-up. Under a spike, every thread was blocked waiting for a connection, and every connection belonged to a request waiting for a thread. Nothing moved until SQLAlchemy's 30 s pool timeout fired.

The 50-user load test never hit this: there were always threads free.

**Fix.** Never hold a connection while waiting for a thread:

* Each endpoint closes its session in the same thread, the moment it returns (`releases_connection` in `app/main.py`).
* API-key lookup uses its own short session, closed before it returns, and hands back a small plain object instead of a database row.

## 2. A cache that still used the database on every hit

**Found by:** `perf/k6/hot_event.js` (100 users, one event's stats)

| | Before | After |
|---|---:|---:|
| Requests failing | 42% | 0% |
| p95 | 30.5 s | 176 ms |

**Cause.** The stats endpoint checked the event existed *before* looking in the cache. Every request waiting for a recompute was holding a connection, so 100 of them emptied a 50-connection pool, even though the cache was doing its job.

**Fix.** Look in the cache first. The existence check only runs when the report is actually recomputed (and a 404 is never cached).

## 3. Latency that depended on when autovacuum ran

**Found by:** comparing repeated k6 runs of the *same* code

| | Before | After |
|---|---:|---:|
| `GET /events` p95, run to run | 58 ms to 2,196 ms | 58 to 68 ms |

**Cause.** The load-test database is bulk-loaded seconds before each run. The event list counts ~20,000 seats per page with an index-only scan, which only skips the table if the visibility map says the rows are visible to everyone. Only VACUUM sets the visibility map. Until autovacuum got around to the new table, each page visited the table for all 20,000 rows. Runs that started before autovacuum finished were slow; runs after it were fast.

I first blamed noise on the shared machine (its load average was high at the time). Logging slow statements showed every slow request was the same query, from the very first second of the run, and `EXPLAIN (ANALYZE, BUFFERS)` showed the heap fetches.

**Fix.** `VACUUM ANALYZE` after loading data (`app/seed.py`). The same advice applies to any production bulk import.

## 4. Five contract bugs found by Schemathesis

Schemathesis generates requests from the OpenAPI spec, including deliberately invalid ones, and checks every response against the spec.

| What it sent | What happened | Fix |
|---|---|---|
| `"price_cents": false` | Accepted: Pydantic turned `false` into `0` | A validator that accepts whole numbers (`3`, `3.0`) and refuses booleans and strings |
| A seat id of 2^63 | Spec allowed it, API refused it: the 2^63-1 limit didn't survive JSON number precision | Limit ids to 2^53, the largest integer every JSON parser holds exactly |
| `"starts_at": 0` | Accepted as a Unix timestamp; the spec says ISO 8601 string | Require a string before parsing |
| `OPTIONS /orders` | 405 with an `Allow` header listing only `GET`, not `POST` | Custom 405 handler that lists every method the path supports |
| Bytes that aren't valid UTF-8 | 400, not in the spec | Documented 400 on endpoints that take a body |

Each one is the kind of mismatch that breaks a client generated from the spec, and none of them showed up in the hand-written tests.

# Missing index

**Switch on:** `PERF_BUGS=missing_index`  
**Caught by:** `test_seats_have_an_index_for_event_and_status_lookups`, the k6 latency budgets, and the CI regression gate (only at realistic data sizes).

## What goes wrong

Nearly every endpoint asks "which seats does this event have, and which are available?":

```sql
SELECT count(*) FROM seats WHERE event_id = :id AND status = 'available'
```

Postgres does **not** index foreign keys on its own. Without an index on `seats(event_id, status)`, each of those lookups reads the whole seats table.

## How it was caught

* **pytest:** checks the index exists and starts with `(event_id, status)`. This is a structural check on purpose. An `EXPLAIN`-based test was tried first and turned out flaky: on a small test table the planner sometimes skips a perfectly good index.
* **k6 and the CI gate:** catch the slowdown, but only with enough data. At 100,000 seats the gate passed the missing index by 0.1 ms on one endpoint. At 1,000,000 seats it fails loudly. The gate now seeds 1,000,000 seats.

## Before and after

1,000 events, 1,000,000 seats, 50 virtual users:

| | Fixed | No index | Change |
|---|---:|---:|---:|
| `GET /events` p95 | 58 ms | 2,620 ms | 45x |
| `GET /events/{id}` p95 | 37 ms | 1,549 ms | 42x |
| `POST /orders` p95 | 109 ms | 836 ms | 7.7x |
| Throughput | 177 req/s | 41 req/s | -77% |

The same comparison at 100,000 seats was only 2-3x. The problem grows with the table, which is how it gets past teams whose test databases are small.

## The fix

```sql
CREATE INDEX ix_seats_event_status ON seats (event_id, status, id);
```

`id` on the end means the seat map (`ORDER BY id`) can be read straight from the index in order, and seat counts become index-only scans.

## Takeaway

Load test against production-sized data. A gate that passes on a tiny database is measuring the database's size, not the code.

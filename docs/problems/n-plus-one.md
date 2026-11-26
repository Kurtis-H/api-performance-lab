# N+1 queries

**Switch on:** `PERF_BUGS=n_plus_one`  
**Caught by:** `test_event_list_query_count_does_not_grow_with_page_size`, the k6 query budget on `GET /events`, and the CI regression gate.

## What goes wrong

`GET /events` shows each event with how many seats are still available. The broken version loads the page of events, then asks the database for one event's seat count at a time:

```python
for event in events_on_this_page:  # 20 events
    available = count_available_seats(event.id)  # one query each
```

That is 2 queries for the page plus 20 more, one per event. It's invisible in development with three events, and it grows with every row you show.

## How it was caught

Every response carries an `X-DB-Queries` header, counted by a SQLAlchemy hook. Query counts don't depend on how fast the machine is, so they make a gate with no noise:

* **pytest:** `limit=2` and `limit=20` must run the same number of queries, and no more than 3.
* **k6:** every endpoint has a query budget (`db_queries{name:GET /events}: max<=3`).
* **CI gate:** any increase in max queries over the saved baseline fails the build.

## Before and after

1,000 events, 1,000,000 seats, 50 virtual users, 40 s steady load:

| | Fixed | N+1 | Change |
|---|---:|---:|---:|
| Queries per `GET /events` | 2 | 22 | 11x |
| `GET /events` p50 | 22 ms | 92 ms | 4.2x |
| `GET /events` p95 | 58 ms | 245 ms | 4.2x |
| Throughput (all endpoints) | 177 req/s | 152 req/s | -14% |

Every other endpoint slowed too (`GET /events/{id}` p95 37 → 98 ms), because 22 round trips per page tie up threads and connections that other requests need.

## The fix

Count seats for the whole page in one statement:

```sql
SELECT e.id, e.name, e.venue, e.starts_at, count(s.id) AS available
FROM (SELECT * FROM events ORDER BY id LIMIT :limit OFFSET :offset) e
LEFT JOIN seats s ON s.event_id = e.id AND s.status = 'available'
GROUP BY e.id, e.name, e.venue, e.starts_at
ORDER BY e.id
```

## Takeaway

Assert on query counts, not only on response times. Latency gates need tolerance for noisy CI machines; a query count of 22 where it used to be 2 needs none.

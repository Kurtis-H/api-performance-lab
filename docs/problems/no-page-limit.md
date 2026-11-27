# No page size limit

**Switch on:** `PERF_BUGS=no_page_limit`  
**Caught by:** `test_list_endpoints_cap_page_size`, the Postman collection (API4 check) and the k6 latency budgets.

## What goes wrong

List endpoints accept any `limit`, and leaving it out returns *everything*. An event's seat map is 1,000 seats. `GET /events?limit=100000` returns all events with a seat count each. This is OWASP API4 (unrestricted resource consumption): one request can make the server do unbounded work.

## How it was caught

The first load test missed it completely, because the k6 script always sent `limit=50`. Real front ends often rely on the server's default instead, so the seat-map request in the traffic mix now leaves `limit` off, like those clients do.

* **pytest:** `limit=100000` must be a 422, and no `limit` must give a page of 20.
* **k6:** a client that doesn't send `limit` gets 1,000 seats per seat map instead of 20, and the latency budgets fail.

## Before and after

50 virtual users, 1,000,000 seats:

| | Fixed (default 20, max 100) | No limit |
|---|---:|---:|
| `GET /events/{id}/seats` p95 | 41 ms | 379 ms |
| `GET /events` p95 | 58 ms | 348 ms |
| Memory after 1 min | 90 MB | 141 MB |
| Throughput | 177 req/s | 109 req/s |

The seat map isn't the only casualty. Every endpoint slowed down, because the CPU was busy serializing 1,000-seat pages for everyone.

## The fix

```python
limit: Annotated[int, Query(ge=1, le=100)] = 20
```

## Takeaway

Test the defaults, not just the parameters your test always sends. And include the clients that send nothing at all.

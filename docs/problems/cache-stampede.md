# Cache stampede

**Switch on:** `PERF_BUGS=cache_stampede`  
**Caught by:** `test_expired_stats_are_recomputed_once_under_load` and `perf/k6/hot_event.js`.

## What goes wrong

`GET /events/{id}/stats` is an expensive report (standing in for a heavy reporting query, it takes ~100 ms), so it's cached for 5 seconds. When the cache expires on a popular event, the broken version lets *every* waiting request recompute it at the same time.

## How it was caught

Latency **does not** show it. In the flash-crowd test, p95 was 176 ms fixed and 167 ms with the stampede. The damage is on the database, so the test counts the work instead:

* `stats_recomputes_total` is a Prometheus counter.
* **pytest:** 20 concurrent requests for a cold event must cause exactly one recompute.
* **k6 `hot_event.js`:** 100 users refresh one event's stats for 40 s. The budget is one recompute per cache window (`ceil(40 / 5) + 2 = 10`).

## Before and after

100 virtual users, one hot event, 40 s:

| | Fixed (single flight) | Stampede |
|---|---:|---:|
| Report recomputes | 8 | 310 |
| Database time spent on the report | ~0.8 s | ~31 s |
| p95 latency | 176 ms | 167 ms |

Here the database had room to spare, so users didn't notice. On a shared production database, 39x the reporting load at every cache expiry is exactly what turns one popular event into a site-wide slowdown.

## The fix

"Single flight": one request recomputes while the others wait for its result (`app/cache.py`):

```python
with self._lock_for(key):
    hit, value = self._fresh(key)  # someone may have filled it while we waited
    if hit:
        return value
    value = compute()
    self._store(key, value)
```

## A second bug found on the way

The first fixed version still failed the flash-crowd test with **42% errors**. Each request checked the event existed (taking a database connection) *before* waiting on the cache lock. So 100 waiting requests held 50 connections between them, and the pool ran dry. The cache is now checked before the database is touched. See [findings](../findings.md).

## Takeaway

Measure the work a cache is meant to save, not just response times. And check what a request holds while it waits.

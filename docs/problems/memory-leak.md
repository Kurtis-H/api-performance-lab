# Memory leak

**Switch on:** `PERF_BUGS=memory_leak`  
**Caught by:** `test_debug_buffer_stays_bounded` and the k6 soak test (`perf/k6/soak.js`).

## What goes wrong

To help debugging, the API keeps "the last few" seat maps it served. The broken version keeps them in a plain list, so it holds every one ever served:

```python
debug_buffer = []  # broken: grows forever
debug_buffer = deque(maxlen=100)  # fixed: keeps the last 100
```

## How it was caught

A normal load test **does not** catch this. After a one-minute load run, memory was 110 MB against 90 MB fixed. That's easy to dismiss as caches warming up.

* **pytest:** serve 150 seat maps, then read the `debug_buffer_entries` gauge from `/metrics`. It must not exceed 100.
* **k6 soak:** steady traffic for a long time, comparing memory after warm-up with memory at the end. The threshold is a *rate* (MB per minute), so it works for a 3-minute check and an 8-hour soak alike. The first version used a fixed 50 MB limit, and a real leak slipped under it at 49.1 MB in a short run.

## Before and after

3-minute soak, 30 virtual users, 1,000,000 seats:

| | Fixed | Leak |
|---|---:|---:|
| Memory at start → end | 81.5 → 84.9 MB | 82.2 → 132.1 MB |
| Growth rate | 1.1 MB/min | 16.4 MB/min |
| Debug buffer entries (1-min load run) | 100 | 3,314 |
| Latency | unchanged | unchanged |

At 16 MB/min the process gains about 1 GB in 1 hour. Latency stays normal the whole time, until the process is killed for running out of memory.

## Takeaway

Some problems need time, not load. Measure memory as a growth rate across a soak, after warm-up, and expose your caches' sizes as metrics.

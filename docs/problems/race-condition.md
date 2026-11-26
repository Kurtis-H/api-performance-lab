# Double booking (race condition)

**Switch on:** `PERF_BUGS=race`  
**Caught by:** `test_fifty_buyers_one_seat_exactly_one_wins` (pytest) and `perf/k6/race.js`.

## What goes wrong

Buying a seat checks it's available, charges the payment provider, then marks it sold:

```python
seat = load(seat_id)  # no lock
if seat.status != "available":
    raise Conflict
charge_payment()  # ~50 ms at a real provider
mark_sold(seat_id)  # unconditional
```

Everyone who reads the seat before the first buyer commits sees "available", so they all pay and they all get the same seat.

## How it was caught

**Load tests don't catch it.** In 40 s at 50 users with 1,000,000 seats, buyers almost never picked the same seat. Zero seats were oversold. A race needs contention, so the tests create it on purpose:

* **pytest:** 50 threads wait at a barrier, then all buy the same seat. Exactly one may get 201 and the other 49 must get 409. The database must show one seat sold.
* **k6 `race.js`:** 50 virtual users line up on a shared start time and buy the last seat. Thresholds are `orders_won == 1` and `orders_lost == 49`.

## Before and after

| | Fixed | Race |
|---|---:|---:|
| Buyers who got the last seat (pytest, 5 runs) | 1, 1, 1, 1, 1 | 40, 40, 39, 40, 35 |
| Buyers who got the last seat (k6) | 1 | 40 |
| Seats oversold in a 40 s normal load test | 0 | 0 |

## The fix

Lock the rows before checking them, in id order so two multi-seat orders can't deadlock each other:

```python
seats = session.execute(
    select(Seat).where(Seat.id.in_(ids)).order_by(Seat.id).with_for_update()
).scalars()
```

The second buyer waits on the lock, then sees the seat as sold and gets a 409.

## Takeaway

A clean load test is not evidence that there are no races. Write the contention test deliberately: same resource, same instant, then count the winners.

# Two buyers holding the same seat

**Switch on:** `PERF_BUGS=hold_race`  
**Caught by:** `test_twenty_buyers_hold_one_seat_at_once_exactly_one_gets_it` (pytest)

## What goes wrong

Holding a seat locks it, then checks nobody else holds it. It's tempting to save a query by reading the holds in the same statement that takes the lock:

```sql
SELECT seats.*, seat_holds.user_id, seat_holds.expires_at
FROM seats LEFT JOIN seat_holds ON seat_holds.seat_id = seats.id
WHERE seats.id = ANY(:ids)
FOR UPDATE OF seats
```

Under Postgres's default isolation (READ COMMITTED), a statement that waits for a row lock re-checks only the locked row when it gets it. The joined holds still come from the snapshot taken before it waited. The second buyer waits for the first one's lock, then reads "no holds" and holds the seat too.

## How it's caught

20 buyers hold the same seat at the same instant; exactly one may succeed. The broken version doesn't fail every time (it let 2 or more holders through in about two rounds out of three), so the test runs five rounds.

| | Fixed | Hold race |
|---|---:|---:|
| Holders who got the seat, in the first round that went wrong (5 runs) | 1 in every round | 2, 9, 11, 5, 7 |

## The fix

Lock the seats, then check the holds in a statement of its own. A new statement takes a new snapshot, which includes the hold committed while we waited. It costs one query.

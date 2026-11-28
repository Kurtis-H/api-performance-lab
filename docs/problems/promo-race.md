# A one-use promo code used many times

**Switch on:** `PERF_BUGS=promo_race`  
**Caught by:** `test_a_single_use_code_is_used_once_however_many_try_at_once` (pytest)

## What goes wrong

A promo code with `max_uses` is checked, then its use is counted when the order commits:

```python
promo = load(code)  # no lock
if promo.uses >= promo.max_uses:
    raise Conflict("That promo code has been used up")
...
promo.uses += 1
```

Every order that reads the code before the first one commits sees it unused, so a single-use 50% code is spent 20 times. The counter ends at 1, so nothing looks wrong afterwards.

## How it's caught

20 buyers use a single-use code at the same instant. Exactly one order may succeed.

A first version of this test let every buyer order the same product, and it passed even with the bug: the product's row lock lined the orders up by itself. Each buyer now orders a different product, so only the code's own lock is being tested. `scripts/prove_problems.py` is what found this, by requiring the test to fail with the bug on.

| | Fixed | Promo race |
|---|---:|---:|
| Orders that got the single-use discount (5 runs) | 1, 1, 1, 1, 1 | 19, 16, 18, 18, 20 |

## The fix

Lock the code's row (`SELECT ... FOR UPDATE`) when an order uses it, after the seats and products. A second order waits, then sees the code used up.

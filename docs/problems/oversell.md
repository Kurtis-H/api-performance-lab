# Overselling the last item

**Switch on:** `PERF_BUGS=oversell`  
**Caught by:** `test_thirty_buyers_one_shirt_left_exactly_one_gets_it` (pytest)

## What goes wrong

An order reads a product's stock, checks there's enough, charges the payment provider, then takes the stock:

```python
product = load(product_id)  # no lock
if quantity > product.stock:
    raise Conflict("Only 1 of Tour T-shirt left")
charge_payment()
product.stock -= quantity
```

Every buyer who reads the stock before the first one commits sees "1 left", so they all pay for the same last shirt. The stock column doesn't even go negative: each order writes `1 - 1 = 0`, so the books look fine while 14 people wait for one shirt.

## How it's caught

30 threads wait at a barrier, then all buy the last shirt. Exactly one may get 201, the other 29 must get 409, and the stock must end at 0.

| | Fixed | Oversell |
|---|---:|---:|
| Buyers who got the last shirt (5 runs) | 1, 1, 1, 1, 1 | 14, 13, 15, 10, 14 |

## The fix

Lock the product rows before checking stock, in id order so two orders for several products can't deadlock. Products are locked after seats and the buyer, and before the promo code: every order takes locks in that one order.

```python
products = session.scalars(
    select(Product).where(Product.id.in_(ids)).order_by(Product.id).with_for_update()
)
```

# Products schema extensions

## Product price history

`V021..V024` add `products_schema.product_price_history`, an append-only audit
table with one row per real price change: old and new price, the verified user
and the instant, ordered per product by `sequence_no`. `products_writer` (and
so `products_app`) can only `SELECT` and `INSERT` on it, `products_reader` only
`SELECT`; the default privileges of the other tables are unchanged. The design,
the ordering rule and the accepted costs are in
[price-history-design.md](price-history-design.md); each migration has a
mirrored `U021..U024__*.rollback.sql`.

```bash
PRODUCTS_TEST_DISPOSABLE=1 python3 tests/test_price_history.py
```

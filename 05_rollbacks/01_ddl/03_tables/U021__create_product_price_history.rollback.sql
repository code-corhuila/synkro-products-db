-- Destroys every price-history row. For disposable environments and for
-- recovery before any history exists; it is not a data-preserving downgrade.
DROP TABLE IF EXISTS products_schema.product_price_history;

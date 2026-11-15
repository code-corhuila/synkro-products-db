-- Run with psql -f, outside an explicit transaction. Preserve every row.
SET lock_timeout = '5s';
ALTER TABLE IF EXISTS products_schema.stock_reservation_line
  DROP CONSTRAINT IF EXISTS uq_stock_reservation_line_reservation_product;
-- Also handles an interrupted concurrent build before the constraint attached.
DROP INDEX CONCURRENTLY IF EXISTS products_schema.uq_stock_reservation_line_reservation_product;
RESET lock_timeout;

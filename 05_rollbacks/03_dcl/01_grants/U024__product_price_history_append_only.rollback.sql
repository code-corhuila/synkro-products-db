-- Restores the privileges the table inherits by default (V003: SELECT, INSERT,
-- UPDATE for products_writer; V020: SELECT for products_reader). Run it with
-- the same effective creator that applied V024, before U021 drops the table.
-- It is skipped when the table is already gone.
DO $$
BEGIN
  IF to_regclass('products_schema.product_price_history') IS NOT NULL THEN
    GRANT UPDATE ON TABLE products_schema.product_price_history TO products_writer;
  END IF;
END
$$;

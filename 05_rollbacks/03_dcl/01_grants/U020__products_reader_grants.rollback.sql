-- Use the same effective creator that applied V020, before U019 drops the role.
ALTER DEFAULT PRIVILEGES IN SCHEMA products_schema
  REVOKE SELECT ON TABLES FROM products_reader;
REVOKE SELECT ON ALL TABLES IN SCHEMA products_schema FROM products_reader;
REVOKE USAGE ON SCHEMA products_schema FROM products_reader;

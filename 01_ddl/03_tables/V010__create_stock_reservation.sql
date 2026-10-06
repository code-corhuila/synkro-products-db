CREATE TABLE products_schema.stock_reservation (
    reservation_id UUID PRIMARY KEY,
    status TEXT NOT NULL
        CONSTRAINT ck_stock_reservation_status CHECK (status IN ('RESERVED', 'RELEASED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at TIMESTAMPTZ
);

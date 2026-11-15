# Schema

Detailed procedures and delivery notes moved from the repository README at
[source 0867b1e](https://github.com/code-corhuila/synkro-products-db/blob/0867b1e98e65ed3c24a170248961ef04d1a44a89/README.md).
Recorded execution results retain their original scope; moving them does not
constitute a new execution or certify a different revision.

Run commands from the repository root unless a step explicitly changes directory.

## Layout

The team groups Flyway scripts by SQL family. Every family folder exists from day one,
even when empty (`.gitkeep`), so execution order never has to be renumbered.

| Folder | Content | Run by |
|---|---|---|
| `01_ddl/` | schemas, types, tables, alters, views, functions, procedures, triggers, indexes | Flyway |
| `02_dml/` | inserts, updates, deletes, upserts, patches | Flyway |
| `03_dcl/` | roles, grants, row-level policies | Flyway |
| `04_tcl/` | transaction blocks, manual recoveries, release tags | Flyway |
| `05_rollbacks/` | one `U` script per `V` migration, mirroring its folder | `psql` only (CI and manual recovery) |
| `deploy/compose.yml` | the migration runner (no database: that is `synkro-infra-postgres`) | Docker Compose |

Rules every new migration follows:

- **One version sequence for the whole repository** — `V001`, `V002`, `V003`, … — whatever folder
  the file lives in. Take the next free number; never renumber.
- **Name:** `V<NNN>__<description>.sql` (two underscores). `validateMigrationNaming` rejects anything else.
- **Every `V` has a `U`** with the same number and description, at the mirrored path under
  `05_rollbacks/` (e.g. `03_dcl/01_grants/V003__grants.sql` → `05_rollbacks/03_dcl/01_grants/U003__grants.sql`).
- **Rollbacks run highest version to lowest.** `U003` (revoke) must run before `U002` (drop role):
  PostgreSQL refuses to drop a role that still holds privileges.
- `05_rollbacks/` is deliberately **not** in `flyway.toml` `locations`: Flyway Community never runs `U` scripts.
- The connection is never in `flyway.toml`; Flyway reads `FLYWAY_URL`, `FLYWAY_USER`, `FLYWAY_PASSWORD`
  from the environment. The runner uses the instance administrator's credentials (ADR-012).
- `products_app` (the login role) is created by `synkro-infra-postgres`'s bootstrap, not here.
  `V003` grants `products_writer` to it, so **it must exist before the first migrate**.
- **Name every constraint**: `ck_<table>_<rule>`, `fk_<table>_<target>`, `uq_<table>_<rule>`.
  The CI checks assert on these names.
- **A status with a conditional timestamp is two implications, never an OR of the valid cases**
  (`stock_reservation.released_at`, `stock_alert.resolved_at`):
  `(status <> 'OPEN' OR resolved_at IS NULL) AND (status <> 'RESOLVED' OR resolved_at IS NOT NULL)`.
  `(OPEN AND NULL) OR (RESOLVED AND NOT NULL)` accepts the same rows but is also false for any
  invalid status, so a bad status gets reported by the wrong constraint.
- **A column added later to an existing table is nullable when old rows cannot be backfilled**
  (`stock_adjustment.stock_after`, `V017`, part of the HU-PRO-09 scope: the idempotent replay of
  `POST /products/{id}/stock-adjustments` must return the stock right after that adjustment).
  Rows predating `V017` stay `NULL`; new rows always carry it. Its CHECK is
  `ck_stock_adjustment_stock_after_non_negative` (`stock_after IS NULL OR stock_after >= 0`), and
  CI asserts it with a valid control (`0`) and a rejected `-1`.
- **A partial unique index that encodes a domain rule** (one active category per name, one OPEN
  `stock_alert` per product) lives in the table's own migration, not in `10_indexes/`. CI tests it
  both ways: the duplicate is rejected, and the row is accepted again once the first leaves the
  predicate (category deactivated, alert resolved).

## Approved reservation integrity and reader access (HU-PRO-15)

V018 enforces `UNIQUE (reservation_id, product_id)` on
`products_schema.stock_reservation_line`, named
`uq_stock_reservation_line_reservation_product`. Its duplicate preflight aborts
without changing rows. If it reports duplicates, stop and agree treatment with
the Products owner/reviewer; no deletion, repair or retry is automatic.
The backing index builds concurrently; its `.sql.conf` disables the transaction
for that file. An invalid leftover index also stops the migration for review.

V019/V020 add `products_reader`: NOLOGIN, no password, schema USAGE and SELECT on
the seven business tables listed in [the design](approved-products-integrity-design.md).
The Flyway ledger is excluded; no login or role membership is added. Unsafe
pre-existing attributes, memberships or effective permissions stop adoption.
Default SELECT privileges apply to future tables created by the **effective
migration creator**. Other creators need their own reviewed defaults; a future
administrative table needs explicit exclusion in its creating migration.

Each new migration has a mirrored `U018..U020__*.rollback.sql`. Run reversions
highest first with `psql -f`, outside a transaction, as the same effective
creator that applied the grants. Never change an applied V/U script.

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

CI requires the real-engine suite:

```bash
PRODUCTS_TEST_DISPOSABLE=1 python3 tests/test_approved_integrity.py
```

It owns and removes isolated Docker resources. It covers clean installation,
populated upgrade, unchanged re-execution, duplicate rejection/preflight,
effective reader permissions and defaults, partial/full rollback and rebuild.
See [verification](evidence/approved-integrity-verification.json) and
[full rebuild](evidence/full-ci-rebuild.json). TD-018 remains open for its
five excluded differences, coordinated by SergioAndres17 without a review date.

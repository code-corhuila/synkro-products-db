# Approved Products integrity (HU-PRO-15, TD-018): design

Status: design written before any SQL. Nothing described here has been
executed yet; see "Pending executions".

## Scope

In scope, and nothing else:

1. `UNIQUE (reservation_id, product_id)` on `products_schema.stock_reservation_line`,
   named `uq_stock_reservation_line_reservation_product`. It enforces the
   existing `StockReservation` invariant (one line per product in a reservation)
   in the database, as the domain model already does in memory.
2. A `products_reader` role: `NOLOGIN`, no password, `USAGE` on the schema,
   `SELECT` on the Products business tables, including tables a future
   migration creates.

Out of scope and untouched: every other `ck_`/`chk` rename, primary-key naming,
foreign-key delete convention, length checks and the product-name/stock
indexes. They stay open, owned by SergioAndres17, without a review date.
TD-012 is excluded: no plan, no test, no code. `V001..V017` and the old `U`
scripts are never edited.

Pattern: migration constraints enforce an aggregate rule that already exists;
roles and grants follow the least-privilege convention of `V002`/`V003`.

## Planned migrations

Versions currently end at 17; free versions are rechecked before writing.

| Version | Location | Purpose |
|---|---|---|
| V018 | `01_ddl/04_alter` | duplicate preflight, concurrent unique index, attach as constraint |
| V019 | `03_dcl/00_roles` | create `products_reader`, idempotently and safely |
| V020 | `03_dcl/01_grants` | schema `USAGE`, `SELECT` on the seven tables, default privileges |

Each has a mirrored `U0NN__<name>.rollback.sql` under `05_rollbacks`.

### V018, concurrency and partial failure

- Preflight: `GROUP BY reservation_id, product_id HAVING count(*) > 1`. Any hit
  aborts with one fixed message that names the table and says "duplicate". It
  carries no row data. Nothing is deleted or merged automatically.
- The unique index is built with `CREATE UNIQUE INDEX CONCURRENTLY`, then
  attached with `ADD CONSTRAINT ... UNIQUE USING INDEX`, so existing data is
  never indexed under a blocking lock. The index is the physical backing of the
  constraint, not an extra business index.
- `CONCURRENTLY` cannot run in a transaction: a per-file
  `V018__<name>.sql.conf` sets `executeInTransaction=false`. The existing
  `transactional.lock=false` stays.
- Re-running the file on an already valid database is safe (the attach runs
  only when needed).
- An invalid leftover index of the same name, as an interrupted concurrent
  build leaves, makes the migration FAIL with guidance. It is neither accepted
  nor silently repaired. The operator drops it (or runs `U018`) and re-applies.
  No forced `flyway repair` on persistent environments.

### V019/V020, roles and grants

- An existing `products_reader` that is not `NOLOGIN`, passwordless and free of
  dangerous attributes makes the migration fail; it is never granted access.
  A safe existing one is adopted.
- No login role is created by a migration and nothing is a member of the role.
- `V020` grants only to the seven reviewed tables below, never to the ledger,
  and sets default privileges for the **current effective migration creator**
  (`current_user`), not a hardcoded `postgres`.
- `U020` revokes the schema, table and default grants (including those on tables
  created later) so that `U019` can drop the role.

### Seven-table classification

Presented to the owner before any grant. Anything else needs a new review.

| Table | Class |
|---|---|
| category | business |
| product | business |
| idempotency_key | business-operation support |
| stock_adjustment | business |
| stock_reservation | business |
| stock_reservation_line | business |
| stock_alert | business |
| flyway_schema_history | administrative, excluded |

### Effective creator and default privileges

`ALTER DEFAULT PRIVILEGES` is per creating role. A future table created by a
different role than the one that ran `V020` is not covered. That is the
caveat, not a defect: the migrations run as one effective creator. The tests
use `pro15_migrator`, deliberately not `postgres`, and assert the owner and the
`pg_default_acl` `defaclrole`.

PostgreSQL's system-catalog and `PUBLIC` metadata access is inherited and is
not claimed to be forbidden. The tests check other domains' *business* schemas.

### Rollback discovery

Rollbacks are applied with `psql -f` (outside an explicit transaction, so the
concurrent `DROP INDEX` of `U018` works). CI discovers them with
`find 05_rollbacks -name 'U*.sql'`, sorted reverse on the file name. The new
`U018__...rollback.sql` names sort above `U017` and in descending order; the
tests prove that rather than renaming historical scripts.
`U018` drops the constraint and any partial owned index; it never touches rows.

## Test map

`tests/test_approved_integrity.py`: stdlib `unittest` driving real PostgreSQL
16.15 and Flyway 11.20.3 through the docker CLI. Per-case isolated clusters
(roles are cluster-global); the clean-install class shares one cluster whose
tests leave no state behind.

| Requirement | Test |
|---|---|
| clean install, exact catalog, nothing else changed | `LatestCleanInstall.test_only_the_named_unique_constraint_and_its_index_are_added` |
| 23505 on the exact constraint, valid controls | `...test_duplicate_pair_is_rejected_and_valid_shapes_are_accepted` |
| role attributes, no password, no membership, no new login | `...test_reader_role_is_nologin_passwordless_and_member_of_nothing` |
| `SELECT` on seven tables only | `...test_reader_selects_every_business_table_and_nothing_else_in_the_schema` |
| `INSERT/UPDATE/DELETE` denied 42501 with controls | `...test_reader_cannot_write_any_business_table` |
| no ledger, no other-domain business access | `...test_reader_has_no_ledger_and_no_other_domain_business_access` |
| non-default creator, default ACL, future table | `...test_default_privileges_follow_the_effective_creator_not_postgres` |
| second migrate no-op | `...test_second_migrate_is_a_noop_and_changes_nothing` |
| raw re-execution safe | `...test_reexecuting_the_new_migrations_raw_is_safe` |
| populated V017 upgrade, frozen values | `PopulatedUpgrade.test_upgrade_from_populated_v017_preserves_rows_and_frozen_values` |
| duplicates abort before any change | `DirtyLegacyData.test_duplicates_abort_the_migration_before_any_change_and_are_never_cleaned` |
| invalid partial index fails | `InvalidPartialIndex.test_raw_v018_fails_with_guidance_on_an_invalid_partial_index` |
| `U018` removes a partial index | `InvalidPartialIndex.test_u018_removes_a_partial_owned_index_without_touching_rows` |
| unsafe / safe pre-existing role | `ExistingReaderRole.*` |
| rollback 20 to 18 and reapply | `Rollbacks.test_new_rollbacks_20_to_18_then_reapply_restores_everything` |
| full historical rollback, discovery, rebuild | `Rollbacks.test_full_historical_rollback_discovery_order_and_rebuild` |

Integrity is proven by executing real PostgreSQL and Flyway. No test inspects
migration source text as evidence, and there is no fake engine.

## Methods and limits

- The dirty-duplicate fixture is disposable: destroyed after the evidence, never
  repaired and never promoted.
- The partial rollback test deletes the new ledger rows so Flyway can reapply.
  That is test-only bookkeeping on a disposable cluster, not a procedure for
  persistent environments.
- Not proven here: that `CREATE INDEX CONCURRENTLY` holds no blocking lock under
  concurrent writes (needs a timed, concurrent workload; reviewed manually).
- The exact abort and guidance text is not fixed by this design. The tests
  require "duplicate" plus the table name, and "invalid" plus the index name.
- `sort -r` is compared in plain code-point order, not the CI runner's locale.
- That `V001..V017` and old `U` scripts are unchanged is verified by the diff
  (`git diff --name-status` shows additions only), not by the tests.

## Safety of the harness

Run with `PRODUCTS_TEST_DISPOSABLE=1 python tests/test_approved_integrity.py`.
It refuses to run without that variable. Every container and network carries
`synkro.products.integrity-test=<run id>`; cleanup removes only that run id, in
`addCleanup`, `tearDownModule` and `atexit`. No port is published, no fixed
name is used, and secrets are passed by environment or stdin and redacted from
any output. The repository is copied with `docker cp`, no bind mount.

## Pending executions

None of the following has been run: RED on the baseline, the migrations, the
rollbacks, the harness itself, CI. They are executed and recorded by the
implementer after this document, and results are reported then.

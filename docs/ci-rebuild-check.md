# Ci rebuild check

Detailed procedures and delivery notes moved from the repository README at
[source 0867b1e](https://github.com/code-corhuila/synkro-products-db/blob/0867b1e98e65ed3c24a170248961ef04d1a44a89/README.md).
Recorded execution results retain their original scope; moving them does not
constitute a new execution or certify a different revision.

Run commands from the repository root unless a step explicitly changes directory.

## The CI rebuild check

`.github/workflows/db-ci.yml` runs on every push and PR to `develop`, `qa` and `main`, against an
empty `postgres:16-alpine`, using the same Flyway image as `deploy/compose.yml` (keep both tags equal).
It proves, in order:

1. **Migrate from empty** applies every `V*.sql` in the repository (the applied count is checked:
   Flyway exits 0 with zero migrations when it cannot find its config).
2. **Migrate again** is a no-op (`No migration necessary`).
3. **Privileges:** `products_app` has `USAGE` on `products_schema`, and exactly `SELECT`, `INSERT`,
   `UPDATE` (never `DELETE`) on every table in it. Those table grants come only from `V003`'s
   `ALTER DEFAULT PRIVILEGES`, which covers tables created by the role that ran `V003`: create
   every table through Flyway with the same administrator credentials.
4. **Constraints reject bad rows for the right reason:** every named CHECK, unique index and
   foreign key has an assertion (`expect_violation <SQLSTATE> <constraint> <SQL>`) that must fail
   with that SQLSTATE *and* that constraint name, and every table has a valid-insert control.
   A bare non-zero exit is not enough: once a column is added, every bad-row INSERT could fail
   on that column instead. When a migration adds a constraint, add its assertion to that step;
   when it adds a required column, update the test INSERTs (the control fails until you do).
   Clean up child rows before parents at the end of the step.
5. **Rollbacks** apply cleanly, highest version to lowest, and leave neither `products_schema`
   nor `products_writer` behind.
6. **Rebuild:** after dropping `flyway_schema_history`, migrating from scratch succeeds again.

To reproduce it by hand against the stand-in database above (bash, repository root):

```bash
docker run --rm --network platform -e PGPASSWORD=dev_admin_pw postgres:16-alpine psql -h synkro-db -U postgres -d synkro -tAc "SELECT has_schema_privilege('products_app', 'products_schema', 'USAGE');"
```

```bash
find 05_rollbacks -name 'U*.sql' -printf '%f\t%p\n' | sort -r | cut -f2 | while read -r f; do echo "Applying $f"; docker exec -i synkro-db psql -U postgres -d synkro -v ON_ERROR_STOP=1 < "$f" || break; done
```

```bash
docker exec synkro-db psql -U postgres -d synkro -c "DROP TABLE IF EXISTS flyway_schema_history;"
```

Then migrate again (step 3 above). The first command must print `t`; the loop must apply every `U` script from the highest
version down to `U001` (today `U017` … `U001`) without errors.

To run the whole workflow locally, unchanged, use [`act`](https://github.com/nektos/act):

```bash
act push -W .github/workflows/db-ci.yml -P ubuntu-latest=catthehacker/ubuntu:act-latest
```

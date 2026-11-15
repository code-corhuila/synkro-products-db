# Root composition

Detailed procedures and delivery notes moved from the repository README at
[source 0867b1e](https://github.com/code-corhuila/synkro-products-db/blob/0867b1e98e65ed3c24a170248961ef04d1a44a89/README.md).
Recorded execution results retain their original scope; moving them does not
constitute a new execution or certify a different revision.

Run commands from the repository root unless a step explicitly changes directory.

## Root composition integration (HU-INF-02)

Flyway uses `products_schema` as its only/default schema and stores its ledger in
`products_schema.flyway_schema_history`. No historical migration was changed.
The tooling runner mounts this repository read-only. Root infrastructure waits
for authenticated PostgreSQL readiness before deliberately invoking it.

If `public.flyway_schema_history` exists in an established environment, stop.
Take a backup, inventory its domain/version/checksum records and obtain a reviewed
reconciliation procedure. Do not repair, move or delete that ledger automatically.
CI drops its disposable schema history **before** the schema-dropping rollback.
Run `python3 tests/test_history_contract.py` for the configuration regression.

"""Product price history tests: constraints, append-only privileges, upgrade, rollback.

Real PostgreSQL 16 and real Flyway through the docker CLI, reusing the
disposable-cluster harness of test_approved_integrity.py. Run it only where
creating and destroying throwaway containers is fine:

    PRODUCTS_TEST_DISPOSABLE=1 python3 tests/test_price_history.py

Written before the migrations V021..V024 (see docs/price-history-design.md).
A missing table, constraint, index or privilege is an assertion failure of the
feature; only the V020 baseline must succeed, otherwise the harness is broken.
"""
import atexit
import os
import pathlib
import re
import shutil
import sys
import tempfile
import unittest

from test_approved_integrity import (
    LEDGER, LOCATIONS, MIGRATOR, ROOT, Cluster, HarnessError, docker, repo_versions,
    rollback_number, rollback_scripts, sweep,
)

APPROVED_LAST = 20  # the last migration that predates price history
HISTORY_LAST = 24
TABLE = "product_price_history"
FULL = f"products_schema.{TABLE}"
PK = "pk_product_price_history"
FK = "fk_product_price_history_product"
UQ = "uq_product_price_history_product_sequence"
INDEX = "idx_product_price_history_product_sequence"
CHECKS = ["chk_product_price_history_sequence_positive", "chk_product_price_history_old_price_positive",
          "chk_product_price_history_new_price_positive", "chk_product_price_history_price_changed"]
BUSINESS_TABLES = ["category", "product", "idempotency_key", "stock_adjustment",
                   "stock_reservation", "stock_reservation_line", "stock_alert"]
USER = "77777777-7777-7777-7777-777777777777"
CAT = "c0000000-0000-0000-0000-000000000001"
P1 = "a0000000-0000-0000-0000-000000000001"
P2 = "a0000000-0000-0000-0000-000000000002"
MISSING = "99999999-9999-9999-9999-999999999999"
AT = "2026-01-01T00:00:00Z"

POPULATE = f"""
INSERT INTO products_schema.category (category_id, name) VALUES ('{CAT}', 'Peripherals');
INSERT INTO products_schema.product (product_id, name, price_cents, stock, category_id) VALUES
  ('{P1}', 'Mouse', 5999, 10, '{CAT}'), ('{P2}', 'Keyboard', 7999, 5, '{CAT}');
INSERT INTO products_schema.stock_adjustment (adjustment_id, product_id, delta, reason, adjusted_by, stock_after)
  VALUES ('e0000000-0000-0000-0000-000000000001', '{P1}', 5, 'Restock', '{USER}', 10);
"""


def history(n, product=P1, seq=None, old=100, new=200, by=USER):
    """One INSERT into the history table; every default is a valid row."""
    seq = n if seq is None else seq
    return (f"INSERT INTO {FULL} (history_id, product_id, sequence_no, old_price_cents, new_price_cents, changed_by, changed_at) "
            f"VALUES ('f0000000-0000-0000-0000-{n:012d}', '{product}', {seq}, {old}, {new}, '{by}', '{AT}')")


_workspace = None


def setUpModule():
    global _workspace
    if os.environ.get("PRODUCTS_TEST_DISPOSABLE") != "1":
        raise RuntimeError("refusing to run: this harness creates and destroys Docker containers; "
                           "set PRODUCTS_TEST_DISPOSABLE=1 on a disposable machine")
    if docker("version", "--format", "{{.Server.Version}}").rc != 0:
        raise HarnessError("the docker daemon is not reachable")
    _workspace = tempfile.mkdtemp(prefix="pro-history-workspace-")
    shutil.copy(ROOT / "flyway.toml", _workspace)
    for location in LOCATIONS:
        shutil.copytree(ROOT / location, pathlib.Path(_workspace) / location)
    atexit.register(sweep)


def tearDownModule():
    try:
        sweep()
    finally:
        if _workspace:
            shutil.rmtree(_workspace, ignore_errors=True)


class HistoryCase(unittest.TestCase):
    def cluster(self):
        cluster = Cluster(_workspace)
        self.addCleanup(cluster.destroy)
        return cluster

    def baseline(self, cluster):
        """Empty cluster -> V001..V020 applied. Must succeed or the harness errors."""
        cluster.start()
        res = cluster.migrate(APPROVED_LAST)
        if res.rc != 0 or cluster.applied() != APPROVED_LAST:
            raise HarnessError(f"baseline migrate through V{APPROVED_LAST:03d} did not succeed:\n{res.out[-800:]}{res.err[-400:]}")

    def assertMigratedToLatest(self, cluster, res):
        expected = len(repo_versions())
        self.assertGreater(expected, APPROVED_LAST, f"no migration after V{APPROVED_LAST:03d} exists in the repository")
        self.assertEqual(res.rc, 0, f"migrate to latest failed:\n{res.out[-1200:]}{res.err[-400:]}")
        self.assertEqual(cluster.applied(), expected, "applied migrations do not match the repository")

    def expect_violation(self, cluster, sqlstate, constraint, sql):
        res = cluster.sql(sql, verbose=True)
        self.assertNotEqual(res.rc, 0, f"accepted, expected {constraint} ({sqlstate}) to reject")
        match = re.search(r"ERROR:\s+(\w{5}):\s+(.*)", res.err)
        self.assertIsNotNone(match, f"no SQLSTATE in the error: {res.err[-400:]}")
        self.assertEqual(match.group(1), sqlstate, f"rejected for the wrong reason: {match.group(0)}")
        self.assertIn(f'"{constraint}"', match.group(2), f"wrong constraint: {match.group(0)}")

    def expect_denied(self, cluster, sql):
        res = cluster.sql(sql, verbose=True)
        self.assertNotEqual(res.rc, 0, f"accepted, expected permission denied: {sql}")
        match = re.search(r"ERROR:\s+(\w{5}):\s+(.*)", res.err)
        self.assertIsNotNone(match, f"no SQLSTATE in the error: {res.err[-400:]}")
        self.assertEqual(match.group(1), "42501", f"not a privilege error: {match.group(0)}")
        self.assertIn(f"permission denied for table {TABLE}", match.group(2))

    def privileges(self, cluster, role, table=TABLE):
        return cluster.js(f"""SELECT coalesce(json_agg(p ORDER BY p), '[]') FROM unnest(ARRAY['SELECT','INSERT','UPDATE',
            'DELETE','TRUNCATE','REFERENCES','TRIGGER']) p
            WHERE has_table_privilege('{role}', 'products_schema.{table}', p)""")

    def history_structure(self, cluster):
        return cluster.js(f"""
        SELECT json_build_object(
          'constraints', (SELECT coalesce(json_agg(json_build_array(conname, contype::text, pg_get_constraintdef(oid))
                 ORDER BY conname), '[]') FROM pg_constraint WHERE conrelid = '{FULL}'::regclass),
          'indexes', (SELECT coalesce(json_agg(json_build_array(indexname, indexdef) ORDER BY indexname), '[]')
                 FROM pg_indexes WHERE schemaname = 'products_schema' AND tablename = '{TABLE}'),
          'columns', (SELECT coalesce(json_agg(json_build_array(column_name, data_type, is_nullable, column_default)
                 ORDER BY ordinal_position), '[]') FROM information_schema.columns
                 WHERE table_schema = 'products_schema' AND table_name = '{TABLE}'))""")


class LatestCleanInstall(HistoryCase):
    """One shared cluster: V001..V020 with data, then latest. Tests here only read,
    or run in rolled-back transactions."""

    @classmethod
    def setUpClass(cls):
        cls.cluster_ = Cluster(_workspace)
        cls.addClassCleanup(cls.cluster_.destroy)
        cls.cluster_.start()
        res = cls.cluster_.migrate(APPROVED_LAST)
        if res.rc != 0:
            raise HarnessError(f"baseline migrate did not succeed:\n{res.out[-800:]}{res.err[-400:]}")
        cls.cluster_.sql_ok(POPULATE)
        cls.latest = cls.cluster_.migrate()

    def setUp(self):
        self.c = self.cluster_
        self.assertEqual(self.latest.rc, 0, self.latest.out[-600:] + self.latest.err[-400:])

    def tx(self, *statements, role=None):
        """A script that is rolled back, so no test leaves state behind."""
        head = "BEGIN;\n" + (f"SET LOCAL ROLE {role};\n" if role else "")
        return head + ";\n".join(statements) + ";\nROLLBACK;"

    def test_all_four_migrations_are_applied(self):
        self.assertEqual(sorted(v for v in repo_versions() if v > APPROVED_LAST), list(range(21, HISTORY_LAST + 1)))
        self.assertMigratedToLatest(self.c, self.latest)

    def test_table_columns_and_named_constraints(self):
        s = self.history_structure(self.c)
        columns = {name: (kind, nullable, default) for name, kind, nullable, default in s["columns"]}
        self.assertEqual(sorted(columns), sorted(["history_id", "product_id", "sequence_no", "old_price_cents",
                                                  "new_price_cents", "changed_by", "changed_at"]))
        self.assertEqual({n: columns[n][0] for n in columns},
                         {"history_id": "uuid", "product_id": "uuid", "sequence_no": "bigint", "old_price_cents": "bigint",
                          "new_price_cents": "bigint", "changed_by": "uuid", "changed_at": "timestamp with time zone"})
        for name, (_, nullable, default) in columns.items():
            self.assertEqual(nullable, "NO", f"{name} must be NOT NULL")
            self.assertIsNone(default, f"{name} must have no DEFAULT: the writer states every value")
        constraints = {name: (kind, definition) for name, kind, definition in s["constraints"]}
        self.assertEqual(sorted(constraints), sorted([PK, FK, UQ, *CHECKS]))
        self.assertEqual(constraints[PK][0], "p")
        self.assertEqual(constraints[UQ], ("u", "UNIQUE (product_id, sequence_no)"))
        self.assertEqual(constraints[FK][0], "f")
        self.assertIn("FOREIGN KEY (product_id) REFERENCES products_schema.product(product_id)", constraints[FK][1])
        self.assertIn("ON UPDATE RESTRICT", constraints[FK][1])
        self.assertIn("ON DELETE RESTRICT", constraints[FK][1])
        for check in CHECKS:
            self.assertEqual(constraints[check][0], "c")

    def test_history_query_index_exists_and_is_valid(self):
        s = self.history_structure(self.c)
        definitions = dict(s["indexes"])
        self.assertIn(INDEX, definitions)
        self.assertIn("(product_id, sequence_no DESC)", definitions[INDEX])
        self.assertEqual(self.c.one(f"""SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
            WHERE c.relname = '{INDEX}' AND i.indisvalid"""), "1")

    def test_a_valid_row_is_accepted_and_each_bad_row_is_rejected_by_its_own_constraint(self):
        # the controls run in the same transaction as the rejected statement's setup
        self.assertEqual(self.c.sql(self.tx(history(1), history(2, old=200, new=300), history(3, product=P2))).rc, 0,
                         "valid history rows must be accepted (control)")
        cases = [
            ("23514", "chk_product_price_history_price_changed", history(4, old=500, new=500)),
            ("23514", "chk_product_price_history_old_price_positive", history(4, old=0, new=500)),
            ("23514", "chk_product_price_history_new_price_positive", history(4, old=500, new=-1)),
            ("23514", "chk_product_price_history_sequence_positive", history(4, seq=0)),
            ("23503", FK, history(4, product=MISSING)),
        ]
        for sqlstate, constraint, bad in cases:
            with self.subTest(constraint=constraint):
                self.expect_violation(self.c, sqlstate, constraint, self.tx(bad))
        with self.subTest(constraint=UQ):
            self.expect_violation(self.c, "23505", UQ, self.tx(history(1), history(2, seq=1)))
        with self.subTest(constraint=PK):
            self.expect_violation(self.c, "23505", PK, self.tx(history(1), history(1, seq=2)))
        with self.subTest(constraint="not null"):
            res = self.c.sql(self.tx(history(1).replace(f"'{USER}'", "NULL")), verbose=True)
            self.assertIn("23502", res.err)

    def test_a_product_with_history_cannot_be_deleted_or_have_its_id_changed(self):
        self.expect_violation(self.c, "23503", FK,
                              self.tx(history(1, product=P2), f"DELETE FROM products_schema.product WHERE product_id = '{P2}'"))
        self.expect_violation(self.c, "23503", FK,
                              self.tx(history(1, product=P2), f"UPDATE products_schema.product SET product_id = '{MISSING}' WHERE product_id = '{P2}'"))
        # control: a product without history can still be deleted by the owner
        self.assertEqual(self.c.sql(self.tx(history(1, product=P2),
                                            "DELETE FROM products_schema.stock_adjustment",
                                            f"DELETE FROM products_schema.product WHERE product_id = '{P1}'")).rc, 0)

    def test_sequence_is_unique_per_product_not_global(self):
        self.assertEqual(self.c.sql(self.tx(history(1, seq=1), history(2, product=P2, seq=1), history(3, seq=2))).rc, 0)

    def test_the_writer_can_only_select_and_insert(self):
        for role in ("products_writer", "products_app"):
            with self.subTest(role=role):
                self.assertEqual(self.privileges(self.c, role), ["INSERT", "SELECT"])
        self.assertEqual(self.c.sql(self.tx(history(1), f"SELECT count(*) FROM {FULL}", role="products_writer")).rc, 0,
                         "the writer must insert and read history (control)")
        for statement in (f"UPDATE {FULL} SET new_price_cents = 1",
                          f"UPDATE {FULL} SET new_price_cents = 1 WHERE false",
                          f"DELETE FROM {FULL}",
                          f"TRUNCATE {FULL}"):
            with self.subTest(statement=statement):
                self.expect_denied(self.c, self.tx(history(1), statement, role="products_writer"))

    def test_the_reader_can_only_select(self):
        self.assertEqual(self.privileges(self.c, "products_reader"), ["SELECT"])
        self.assertEqual(self.c.sql(self.tx(f"SELECT count(*) FROM {FULL}", role="products_reader")).rc, 0)
        for statement in (history(1), f"UPDATE {FULL} SET new_price_cents = 1", f"DELETE FROM {FULL}"):
            with self.subTest(statement=statement):
                self.expect_denied(self.c, self.tx(statement, role="products_reader"))

    def test_other_tables_keep_their_existing_privileges(self):
        for table in BUSINESS_TABLES:
            with self.subTest(table=table):
                self.assertEqual(self.privileges(self.c, "products_writer", table), ["INSERT", "SELECT", "UPDATE"])
                self.assertEqual(self.privileges(self.c, "products_app", table), ["INSERT", "SELECT", "UPDATE"])
                self.assertEqual(self.privileges(self.c, "products_reader", table), ["SELECT"])
        self.assertEqual(self.privileges(self.c, "products_writer", LEDGER), [])

    def test_default_privileges_are_unchanged_so_the_revoke_is_specific_to_this_table(self):
        probe = "pro_history_probe_future"
        # Defaults belong to the effective migration creator, so the probe is created by it.
        self.c.sql_ok(f"CREATE TABLE products_schema.{probe} (id int PRIMARY KEY);", user=MIGRATOR)
        self.addCleanup(self.c.sql_ok, f"DROP TABLE IF EXISTS products_schema.{probe};", MIGRATOR)
        self.assertEqual(self.privileges(self.c, "products_writer", probe), ["INSERT", "SELECT", "UPDATE"],
                         "a future table must still inherit V003's defaults")

    def test_second_migrate_is_a_noop(self):
        res = self.c.migrate()
        self.assertEqual(res.rc, 0, res.out[-600:])
        self.assertIn("No migration necessary", res.out)

    def test_the_table_starts_empty_and_existing_rows_are_intact(self):
        self.assertEqual(self.c.one(f"SELECT count(*) FROM {FULL}"), "0")
        self.assertEqual(self.c.one(f"SELECT price_cents || '/' || stock FROM products_schema.product WHERE product_id = '{P1}'"), "5999/10")


class PopulatedUpgrade(HistoryCase):
    def test_upgrade_from_populated_v020_only_adds_history_objects(self):
        c = self.cluster()
        self.baseline(c)
        c.sql_ok(POPULATE)
        rows = c.rows()
        before = c.structure()
        acl_before = {name: acl for name, acl in before["table_acl"]}

        self.assertMigratedToLatest(c, c.migrate())

        self.assertEqual(c.rows(), rows, "an upgrade must not change a single row")
        after = c.structure()
        for kind in ("constraints", "indexes"):
            removed = [r for r in before[kind] if r not in after[kind]]
            added = [r for r in after[kind] if r not in before[kind]]
            self.assertEqual(removed, [], f"existing {kind} changed")
            self.assertTrue(added, f"no {kind[:-1]} was added")
            for row in added:
                self.assertIn(TABLE, " ".join(str(x) for x in row), f"unrelated {kind[:-1]} added: {row}")
        for name, acl in after["table_acl"]:
            if name != TABLE:
                self.assertEqual(acl, acl_before[name], f"the ACL of {name} changed")
        self.assertEqual(after["default_acl"], before["default_acl"], "default privileges must not change")
        self.assertEqual(after["invalid_indexes"], 0)
        self.assertEqual(c.one(f"SELECT count(*) FROM {FULL}"), "0")
        self.assertIn("No migration necessary", c.migrate().out)


class Rollbacks(HistoryCase):
    def history_rollbacks(self):
        scripts = [p for p in rollback_scripts() if rollback_number(p) > APPROVED_LAST]
        self.assertEqual(sorted(rollback_number(p) for p in scripts), list(range(21, HISTORY_LAST + 1)),
                         "every history V migration needs exactly one mirrored U rollback")
        for path in scripts:
            self.assertRegex(path.name, r"^U\d{3}__.+\.rollback\.sql$")
        names = [p.name for p in scripts]
        self.assertEqual(names, sorted(names, reverse=True), "rollbacks must run highest version first")
        return scripts

    def test_rollback_21_to_24_then_reapply_restores_the_same_structure(self):
        c = self.cluster()
        self.baseline(c)
        c.sql_ok(POPULATE)
        self.assertMigratedToLatest(c, c.migrate())
        latest_structure, latest_history = c.structure(), self.history_structure(c)
        rows = c.rows()
        writer_before = {t: self.privileges(c, "products_writer", t) for t in BUSINESS_TABLES}

        for path in self.history_rollbacks():
            res = c.run_file(path)
            self.assertEqual(res.rc, 0, f"{path.name} failed: {res.err[-400:]}")

        self.assertEqual(c.one(f"SELECT count(*) FROM pg_class WHERE relname = '{TABLE}'"), "0", "U021 must drop the table")
        self.assertEqual(c.one(f"SELECT count(*) FROM pg_class WHERE relname = '{INDEX}'"), "0")
        self.assertEqual(c.rows(), rows, "a rollback must not touch other tables' rows")
        self.assertEqual({t: self.privileges(c, "products_writer", t) for t in BUSINESS_TABLES}, writer_before)
        self.assertEqual(c.structure()["invalid_indexes"], 0)

        # TEST-ONLY bookkeeping on a disposable cluster: forget the ledger rows of the
        # versions just rolled back so Flyway may apply them again.
        c.sql_ok(f"DELETE FROM products_schema.{LEDGER} WHERE version IS NOT NULL AND version::int > {APPROVED_LAST};")
        self.assertMigratedToLatest(c, c.migrate())
        self.assertEqual(c.structure(), latest_structure, "re-applying must reproduce the same structure")
        self.assertEqual(self.history_structure(c), latest_history)
        self.assertEqual(c.rows(), rows)

    def test_the_full_rollback_discovery_removes_the_history_table_before_the_roles(self):
        c = self.cluster()
        self.baseline(c)
        self.assertMigratedToLatest(c, c.migrate())
        c.sql_ok(POPULATE)
        c.sql_ok(history(1))  # history rows exist: the rollback destroys them, by design

        discovered = rollback_scripts()
        names = [p.name for p in discovered]
        self.assertEqual(sorted(rollback_number(p) for p in discovered), sorted(repo_versions()))
        c.sql_ok(f"DROP TABLE IF EXISTS products_schema.{LEDGER};")
        c.sql_ok("DELETE FROM products_schema.stock_adjustment;")
        for path in discovered:
            res = c.run_file(path)
            self.assertEqual(res.rc, 0, f"{path.name} failed: {res.err[-400:]}")

        self.assertEqual(c.one("SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'products_schema'"), "0")
        for role in ("products_writer", "products_reader"):
            self.assertEqual(c.one(f"SELECT count(*) FROM pg_roles WHERE rolname = '{role}'"), "0", f"{role} survived")
        self.assertEqual(names, sorted(names, reverse=True))


if __name__ == "__main__":
    if os.environ.get("PRODUCTS_TEST_DISPOSABLE") != "1":
        sys.exit("refusing to run: set PRODUCTS_TEST_DISPOSABLE=1 (this harness creates and destroys Docker containers)")
    unittest.main(verbosity=2)

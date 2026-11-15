"""Approved-integrity tests: UNIQUE(reservation_id, product_id) and products_reader.

Real PostgreSQL 16 and real Flyway, driven through the docker CLI (standard
library only, no host psql). Run it only against a machine where creating and
destroying throwaway containers is fine:

    PRODUCTS_TEST_DISPOSABLE=1 python tests/test_approved_integrity.py

Every Docker resource carries the label synkro.products.integrity-test=<run id>
and is removed in a finally path; nothing without that run id is ever touched.
No ports are published and no credential appears on a command line or in a
failure message: secrets travel through the environment and stdin, and every
captured output is redacted before it can be printed.

Roles are cluster-global, so each lifecycle that creates or drops them gets its
own cluster. The "latest clean install" class shares one cluster because its
tests only read (or leave the state exactly as they found it).

Missing V018..V020 / U018..U020 files, or a missing role or constraint, are
assertion failures of the feature, never fixture errors. Only the baseline
(migrate through V017 on an empty cluster) must succeed, otherwise the harness
itself is broken and says so.
"""
import atexit
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from collections import namedtuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = "postgres:16.15-alpine3.23"
FLYWAY_IMAGE = "flyway/flyway:11.20.3-alpine"
LABEL = "synkro.products.integrity-test"
RUN_ID = uuid.uuid4().hex[:12]
BASELINE = 17
MIGRATOR = "pro15_migrator"  # the effective migration creator: deliberately not postgres
CONSTRAINT = "uq_stock_reservation_line_reservation_product"
LINE_TABLE = "stock_reservation_line"
BUSINESS_TABLES = ["category", "product", "idempotency_key", "stock_adjustment",
                   "stock_reservation", "stock_reservation_line", "stock_alert"]
LEDGER = "flyway_schema_history"
OTHER_DOMAINS = {"auth_schema": "credential", "customers_schema": "customer",
                 "sales_schema": "sale_order", "workflow_schema": "saga_instance"}
LOCATIONS = ["01_ddl", "02_dml", "03_dcl", "04_tcl"]

SECRETS = set()
Result = namedtuple("Result", "rc out err")


class HarnessError(RuntimeError):
    """The fixture is broken. This is an ERROR, never a feature failure."""


def redact(text):
    for secret in SECRETS:
        text = text.replace(secret, "***")
    return text


def run(args, stdin=None, env=None, timeout=600):
    proc = subprocess.run(args, input=stdin, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env={**os.environ, **(env or {})}, timeout=timeout)
    return Result(proc.returncode, redact(proc.stdout), redact(proc.stderr))


def docker(*args, stdin=None, env=None, timeout=600):
    return run(["docker", *args], stdin=stdin, env=env, timeout=timeout)


def docker_ok(*args, stdin=None, env=None):
    res = docker(*args, stdin=stdin, env=env)
    if res.rc != 0:
        # Only the subcommand is named: the arguments may carry names/labels
        # but the environment is where secrets live, and it is never echoed.
        raise HarnessError(f"docker {args[0]} failed ({res.rc}): {res.err.strip()[-400:]}")
    return res


# ---------------------------------------------------------------- repository

def repo_versions():
    """{version: path} of every V migration in the Flyway locations."""
    found = {}
    for location in LOCATIONS:
        for path in (ROOT / location).rglob("V*.sql"):
            match = re.match(r"V(\d+)__", path.name)
            if match:
                found[int(match.group(1))] = path
    return found


def rollback_scripts():
    """Same discovery as db-ci.yml: find 05_rollbacks -name 'U*.sql' | sort -r
    on the 'name<TAB>path' lines. Returns paths, highest name first."""
    lines = sorted((f"{p.name}\t{p.relative_to(ROOT).as_posix()}"
                    for p in (ROOT / "05_rollbacks").rglob("U*.sql")), reverse=True)
    return [ROOT / line.split("\t", 1)[1] for line in lines]


def rollback_number(path):
    return int(re.match(r"U(\d+)__", path.name).group(1))


# ------------------------------------------------------------------- cluster

class Cluster:
    """One throwaway PostgreSQL 16 + Flyway environment, owned by this run."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.id = f"{RUN_ID}-{uuid.uuid4().hex[:6]}"
        self.network = f"pro15-net-{self.id}"
        self.container = f"pro15-pg-{self.id}"
        self.labels = ["--label", f"{LABEL}={RUN_ID}"]
        self.admin_pw = secrets.token_hex(12)
        self.migrator_pw = secrets.token_hex(12)
        self.app_pw = secrets.token_hex(12)
        SECRETS.update({self.admin_pw, self.migrator_pw, self.app_pw})
        self.started = False

    # lifecycle
    def start(self):
        self.started = True
        docker_ok("network", "create", *self.labels, self.network)
        docker_ok("run", "-d", "--name", self.container, "--network", self.network,
                  "--network-alias", "db", *self.labels,
                  "--tmpfs", "/var/lib/postgresql/data",
                  "-e", "POSTGRES_PASSWORD", "-e", "POSTGRES_DB=products", POSTGRES_IMAGE,
                  env={"POSTGRES_PASSWORD": self.admin_pw})
        deadline = time.time() + 120
        while time.time() < deadline:
            # TCP on loopback only answers once initdb's temporary server is gone.
            res = docker("exec", self.container, "psql", "-h", "127.0.0.1", "-U", "postgres",
                         "-d", "products", "-tAc", "SELECT 1")
            if res.rc == 0 and res.out.strip() == "1":
                break
            time.sleep(1)
        else:
            raise HarnessError("PostgreSQL did not become ready")
        self.sql_ok(f"""
            CREATE ROLE products_app LOGIN PASSWORD '{self.app_pw}';
            CREATE ROLE {MIGRATOR} LOGIN SUPERUSER PASSWORD '{self.migrator_pw}';
            GRANT CREATE ON DATABASE products TO {MIGRATOR};
            """ + "".join(
            f"CREATE SCHEMA {s}; CREATE TABLE {s}.{t} (id int PRIMARY KEY, v text);"
            f"INSERT INTO {s}.{t} VALUES (1, 'x');" for s, t in OTHER_DOMAINS.items()))

    def destroy(self):
        if not self.started:
            return
        docker("rm", "-f", "-v", self.container)
        docker("network", "rm", self.network)

    # psql: statements go through stdin with -f -, never as one -c, so a
    # script is not wrapped in an implicit transaction and CONCURRENTLY works.
    def sql(self, script, user="postgres", verbose=False):
        args = ["exec", "-i", self.container, "psql", "-X", "-q", "-t", "-A",
                "-v", "ON_ERROR_STOP=1", "-U", user, "-d", "products"]
        if verbose:
            args += ["-v", "VERBOSITY=verbose"]
        return docker(*args, "-f", "-", stdin=script)

    def sql_ok(self, script, user="postgres"):
        res = self.sql(script, user)
        if res.rc != 0:
            raise HarnessError(f"fixture SQL failed: {res.err.strip()[-400:]}")
        return res.out

    def one(self, query):
        return self.sql_ok(query).strip()

    def js(self, query):
        return json.loads(self.one(query))

    def run_file(self, path, user=MIGRATOR):
        return self.sql(path.read_text(encoding="utf-8"), user=user)

    # flyway: the repository is copied into the container with docker cp, so
    # no bind mount (and no host path) is involved.
    def flyway(self, *options, user=MIGRATOR):
        password = self.migrator_pw if user == MIGRATOR else self.admin_pw
        cid = docker_ok(
            "create", "--network", self.network, *self.labels,
            "-e", "FLYWAY_URL", "-e", "FLYWAY_USER", "-e", "FLYWAY_PASSWORD", FLYWAY_IMAGE,
            "-workingDirectory=/workspace", "-configFiles=flyway.toml", *options,
            env={"FLYWAY_URL": "jdbc:postgresql://db:5432/products", "FLYWAY_USER": user,
                 "FLYWAY_PASSWORD": password}).out.strip()
        try:
            docker_ok("cp", f"{self.workspace}/.", f"{cid}:/workspace")
            res = docker("start", "-a", cid)
            code = int(docker_ok("inspect", "-f", "{{.State.ExitCode}}", cid).out.strip())
            return Result(code, res.out, res.err)
        finally:
            docker("rm", "-f", "-v", cid)

    def migrate(self, target=None):
        options = [f"-target={target}"] if target else []
        return self.flyway(*options, "migrate")

    def baseline(self):
        """Empty cluster -> V001..V017 applied. Must succeed or the harness errors."""
        self.start()
        res = self.migrate(BASELINE)
        if res.rc != 0 or self.applied() != BASELINE:
            raise HarnessError(f"baseline migrate through V{BASELINE:03d} did not succeed:\n{res.out[-800:]}{res.err[-400:]}")

    # state
    def applied(self):
        return int(self.one(f"SELECT count(*) FROM products_schema.{LEDGER} WHERE success AND version IS NOT NULL"))

    def rows(self):
        parts = ", ".join(
            f"'{t}', (SELECT md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) FROM products_schema.{t} x)"
            for t in BUSINESS_TABLES)
        return self.js(f"SELECT json_build_object({parts})")

    def structure(self):
        return self.js(r"""
        SELECT json_build_object(
          'constraints', (SELECT coalesce(json_agg(json_build_array(conrelid::regclass::text, conname,
                 pg_get_constraintdef(oid)) ORDER BY conrelid::regclass::text, conname), '[]')
                 FROM pg_constraint WHERE connamespace = 'products_schema'::regnamespace),
          'indexes', (SELECT coalesce(json_agg(json_build_array(tablename, indexname, indexdef)
                 ORDER BY tablename, indexname), '[]') FROM pg_indexes WHERE schemaname = 'products_schema'),
          'invalid_indexes', (SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid
                 WHERE c.relnamespace = 'products_schema'::regnamespace AND NOT i.indisvalid),
          'table_acl', (SELECT coalesce(json_agg(json_build_array(relname, relacl::text) ORDER BY relname), '[]')
                 FROM pg_class WHERE relnamespace = 'products_schema'::regnamespace AND relkind = 'r'),
          'schema_acl', (SELECT nspacl::text FROM pg_namespace WHERE nspname = 'products_schema'),
          'default_acl', (SELECT coalesce(json_agg(json_build_array(defaclrole::regrole::text, defaclobjtype::text,
                 defaclacl::text) ORDER BY defaclrole::regrole::text, defaclobjtype::text), '[]')
                 FROM pg_default_acl WHERE defaclnamespace = 'products_schema'::regnamespace),
          'roles', (SELECT coalesce(json_agg(json_build_array(rolname, rolsuper, rolinherit, rolcreaterole,
                 rolcreatedb, rolcanlogin, rolreplication, rolbypassrls, rolpassword IS NULL) ORDER BY rolname), '[]')
                 FROM pg_authid WHERE rolname LIKE 'products\_%'),
          'members', (SELECT coalesce(json_agg(json_build_array(r.rolname, m.rolname) ORDER BY r.rolname, m.rolname), '[]')
                 FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid JOIN pg_roles m ON m.oid = a.member
                 WHERE r.rolname LIKE 'products\_%' OR m.rolname LIKE 'products\_%'))
        """)

    def login_roles(self):
        return self.js("SELECT json_agg(rolname ORDER BY rolname) FROM pg_roles WHERE rolcanlogin")

    def role_exists(self, name):
        return self.one(f"SELECT count(*) FROM pg_roles WHERE rolname = '{name}'") == "1"


# ------------------------------------------------------------------- data

IDS = {  # fixed UUIDs, readable in a failure
    "cat": "c0000000-0000-0000-0000-000000000001",
    "p1": "a0000000-0000-0000-0000-000000000001", "p2": "a0000000-0000-0000-0000-000000000002",
    "r1": "b0000000-0000-0000-0000-000000000001", "r2": "b0000000-0000-0000-0000-000000000002",
    "r3": "b0000000-0000-0000-0000-000000000003",
    "l1": "d0000000-0000-0000-0000-000000000001", "l2": "d0000000-0000-0000-0000-000000000002",
    "l3": "d0000000-0000-0000-0000-000000000003", "l4": "d0000000-0000-0000-0000-000000000004",
    "l9": "d0000000-0000-0000-0000-000000000009",
    "adj": "e0000000-0000-0000-0000-000000000001", "alert": "f0000000-0000-0000-0000-000000000001",
    "user": "77777777-7777-7777-7777-777777777777",
}


def sql_of(template):
    return template.format(**IDS)


# The reservation_line price (4999) deliberately differs from the product's
# current price (5999): a frozen value that an upgrade must not touch.
POPULATE = sql_of("""
INSERT INTO products_schema.category (category_id, name) VALUES ('{cat}', 'Peripherals');
INSERT INTO products_schema.product (product_id, name, price_cents, stock, category_id) VALUES
  ('{p1}', 'Mouse', 5999, 10, '{cat}'), ('{p2}', 'Keyboard', 7999, 5, '{cat}');
INSERT INTO products_schema.stock_adjustment (adjustment_id, product_id, delta, reason, adjusted_by, stock_after)
  VALUES ('{adj}', '{p1}', 5, 'Restock', '{user}', 10);
INSERT INTO products_schema.stock_reservation (reservation_id, status) VALUES ('{r1}', 'RESERVED'), ('{r2}', 'RESERVED');
INSERT INTO products_schema.idempotency_key (idempotency_key, resource_type, resource_id)
  VALUES ('pro15-key-00000001', 'STOCK_RESERVATION', '{r1}');
INSERT INTO products_schema.stock_reservation_line (line_id, reservation_id, product_id, quantity, unit_price_cents) VALUES
  ('{l1}', '{r1}', '{p1}', 2, 4999),   -- r1 holds two distinct products
  ('{l2}', '{r1}', '{p2}', 1, 7999),
  ('{l3}', '{r2}', '{p1}', 1, 4999);   -- p1 again, in another reservation
INSERT INTO products_schema.stock_alert (alert_id, product_id, status, stock_at_opening) VALUES ('{alert}', '{p2}', 'OPEN', 2);
""")

DUPLICATE_LINE = sql_of(
    "INSERT INTO products_schema.stock_reservation_line (line_id, reservation_id, product_id, quantity, unit_price_cents) "
    "VALUES ('{l4}', '{r1}', '{p1}', 3, 4999);")

# One valid statement per table and action. Each is syntactically and
# referentially valid for POPULATE's rows, so any rejection is about privileges.
WRITES = {
    "category": ("INSERT INTO products_schema.category (category_id, name) VALUES ('c0000000-0000-0000-0000-000000000009', 'Other')",
                 "UPDATE products_schema.category SET name = name WHERE false",
                 "DELETE FROM products_schema.category WHERE false"),
    "product": (sql_of("INSERT INTO products_schema.product (product_id, name, price_cents, category_id) VALUES ('a0000000-0000-0000-0000-000000000009', 'Pad', 100, '{cat}')"),
                "UPDATE products_schema.product SET name = name WHERE false",
                "DELETE FROM products_schema.product WHERE false"),
    "idempotency_key": (sql_of("INSERT INTO products_schema.idempotency_key (idempotency_key, resource_type, resource_id) VALUES ('pro15-key-00000009', 'PRODUCT', '{p1}')"),
                        "UPDATE products_schema.idempotency_key SET resource_type = resource_type WHERE false",
                        "DELETE FROM products_schema.idempotency_key WHERE false"),
    "stock_adjustment": (sql_of("INSERT INTO products_schema.stock_adjustment (adjustment_id, product_id, delta, reason, adjusted_by) VALUES ('e0000000-0000-0000-0000-000000000009', '{p1}', 1, 'Restock', '{user}')"),
                         "UPDATE products_schema.stock_adjustment SET reason = reason WHERE false",
                         "DELETE FROM products_schema.stock_adjustment WHERE false"),
    "stock_reservation": ("INSERT INTO products_schema.stock_reservation (reservation_id, status) VALUES ('b0000000-0000-0000-0000-000000000009', 'RESERVED')",
                          "UPDATE products_schema.stock_reservation SET status = status WHERE false",
                          "DELETE FROM products_schema.stock_reservation WHERE false"),
    "stock_reservation_line": (sql_of("INSERT INTO products_schema.stock_reservation_line (line_id, reservation_id, product_id, quantity, unit_price_cents) VALUES ('{l9}', '{r2}', '{p2}', 1, 100)"),
                               "UPDATE products_schema.stock_reservation_line SET quantity = quantity WHERE false",
                               "DELETE FROM products_schema.stock_reservation_line WHERE false"),
    "stock_alert": (sql_of("INSERT INTO products_schema.stock_alert (alert_id, product_id, status, stock_at_opening) VALUES ('f0000000-0000-0000-0000-000000000009', '{p1}', 'OPEN', 1)"),
                    "UPDATE products_schema.stock_alert SET status = status WHERE false",
                    "DELETE FROM products_schema.stock_alert WHERE false"),
}


# ------------------------------------------------------------------- module

_workspace = None
_clusters = []


def sweep():
    """Remove everything this run created, even if a test died half-way."""
    for kind, listing in (("container", ("ps", "-aq")), ("network", ("network", "ls", "-q"))):
        ids = docker(*listing, "--filter", f"label={LABEL}={RUN_ID}").out.split()
        for ident in ids:
            if kind == "container":
                docker("rm", "-f", "-v", ident)
            else:
                docker("network", "rm", ident)


def setUpModule():
    global _workspace
    if os.environ.get("PRODUCTS_TEST_DISPOSABLE") != "1":
        raise RuntimeError("refusing to run: this harness creates and destroys Docker containers; "
                           "set PRODUCTS_TEST_DISPOSABLE=1 on a disposable machine")
    if docker("version", "--format", "{{.Server.Version}}").rc != 0:
        raise HarnessError("the docker daemon is not reachable")
    _workspace = tempfile.mkdtemp(prefix="pro15-workspace-")
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


class IntegrityCase(unittest.TestCase):
    def cluster(self):
        cluster = Cluster(_workspace)
        self.addCleanup(cluster.destroy)
        return cluster

    # --- helpers that turn a missing feature into an assertion failure
    def new_migration(self, folder):
        found = sorted(p for v, p in repo_versions().items() if v > BASELINE and p.parent == ROOT / folder)
        if len(found) != 1:
            self.fail(f"expected exactly one new migration (V{BASELINE + 1:03d}+) in {folder}, found {len(found)}")
        return found[0]

    def assertMigratedToLatest(self, cluster, res):
        expected = len(repo_versions())
        self.assertGreater(expected, BASELINE, f"no migration after V{BASELINE:03d} exists in the repository")
        self.assertEqual(res.rc, 0, f"migrate to latest failed:\n{res.out[-1200:]}{res.err[-400:]}")
        self.assertEqual(cluster.applied(), expected, "applied migrations do not match the repository")

    def expect_violation(self, cluster, sqlstate, constraint, sql):
        """psql -v VERBOSITY=verbose: must fail with this SQLSTATE and constraint."""
        res = cluster.sql(sql, verbose=True)
        self.assertNotEqual(res.rc, 0, f"accepted, expected {constraint} ({sqlstate}) to reject")
        match = re.search(r"ERROR:\s+(\w{5}):\s+(.*)", res.err)
        self.assertIsNotNone(match, f"no SQLSTATE in the error: {res.err[-400:]}")
        self.assertEqual(match.group(1), sqlstate, f"rejected for the wrong reason: {match.group(0)}")
        self.assertIn(f'"{constraint}"', match.group(2), f"wrong constraint: {match.group(0)}")

    def expect_denied(self, cluster, kind, name, sql):
        res = cluster.sql(sql, verbose=True)
        self.assertNotEqual(res.rc, 0, f"accepted, expected permission denied for {kind} {name}")
        match = re.search(r"ERROR:\s+(\w{5}):\s+(.*)", res.err)
        self.assertIsNotNone(match, f"no SQLSTATE in the error: {res.err[-400:]}")
        self.assertEqual(match.group(1), "42501", f"not a privilege error: {match.group(0)}")
        self.assertIn(f"permission denied for {kind} {name}", match.group(2))

    def assertNamedUniqueConstraint(self, cluster):
        cols = cluster.one(f"""
            SELECT coalesce(string_agg(a.attname, ',' ORDER BY k.ord), '')
            FROM pg_constraint c
            CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE c.conname = '{CONSTRAINT}' AND c.contype = 'u'
              AND c.conrelid = 'products_schema.{LINE_TABLE}'::regclass""")
        self.assertEqual(cols, "reservation_id,product_id", f"{CONSTRAINT} is not UNIQUE(reservation_id, product_id)")
        self.assertEqual(cluster.one(f"""
            SELECT count(*) FROM pg_constraint c JOIN pg_index i ON i.indexrelid = c.conindid
            WHERE c.conname = '{CONSTRAINT}' AND i.indisvalid AND i.indisunique"""), "1",
                         "the constraint must be backed by one valid unique index")

    def populate_then_latest(self, cluster):
        cluster.baseline()
        cluster.sql_ok(POPULATE)
        res = cluster.migrate()
        self.assertMigratedToLatest(cluster, res)


# ------------------------------------------------- clean install -> latest

class LatestCleanInstall(IntegrityCase):
    """One shared cluster: V001..V017, then latest. Tests here never leave
    state behind (controls run in rolled-back transactions)."""

    @classmethod
    def setUpClass(cls):
        cls.cluster_ = Cluster(_workspace)
        cls.addClassCleanup(cls.cluster_.destroy)
        cls.cluster_.baseline()  # harness error if the baseline cannot be built
        cls.before = cls.cluster_.structure()
        cls.logins_before = cls.cluster_.login_roles()
        cls.latest = cls.cluster_.migrate()
        cls.after = cls.cluster_.structure()

    def setUp(self):
        self.c = self.cluster_
        self.assertEqual(self.latest.rc, 0, self.latest.out[-600:] + self.latest.err[-400:])

    def test_only_the_named_unique_constraint_and_its_index_are_added(self):
        self.assertNamedUniqueConstraint(self.c)
        for kind in ("constraints", "indexes"):
            added = [r for r in self.after[kind] if r not in self.before[kind]]
            removed = [r for r in self.before[kind] if r not in self.after[kind]]
            self.assertEqual(removed, [], f"existing {kind} changed")
            self.assertEqual(len(added), 1, f"expected only the new {kind[:-1]}, got {added}")
        self.assertEqual(self.after["constraints"].count(
            [f"products_schema.{LINE_TABLE}", CONSTRAINT, "UNIQUE (reservation_id, product_id)"]), 1)
        self.assertIn([LINE_TABLE, CONSTRAINT,
                       f"CREATE UNIQUE INDEX {CONSTRAINT} ON products_schema.{LINE_TABLE} "
                       "USING btree (reservation_id, product_id)"], self.after["indexes"])
        self.assertEqual(self.after["invalid_indexes"], 0)

    def test_duplicate_pair_is_rejected_and_valid_shapes_are_accepted(self):
        prefix = "BEGIN;\n" + POPULATE
        # controls: the same reservation with another product, and the same
        # product in another reservation, are valid
        self.assertEqual(self.c.sql(prefix + sql_of(
            "INSERT INTO products_schema.stock_reservation_line (line_id, reservation_id, product_id, quantity, unit_price_cents) "
            "VALUES ('{l9}', '{r2}', '{p2}', 1, 100);\n"
            "INSERT INTO products_schema.stock_reservation (reservation_id, status) VALUES ('{r3}', 'RESERVED');\n"
            "INSERT INTO products_schema.stock_reservation_line (line_id, reservation_id, product_id, quantity, unit_price_cents) "
            "VALUES ('{l4}', '{r3}', '{p1}', 1, 100);\nROLLBACK;")).rc, 0)
        self.expect_violation(self.c, "23505", CONSTRAINT, prefix + DUPLICATE_LINE + "\nROLLBACK;")

    def test_reader_role_is_nologin_passwordless_and_member_of_nothing(self):
        roles = {r[0]: r for r in self.after["roles"]}
        self.assertIn("products_reader", roles, "products_reader does not exist")
        # name, super, inherit, createrole, createdb, login, replication, bypassrls, password IS NULL
        _, sup, _, createrole, createdb, login, repl, bypass, no_password = roles["products_reader"]
        self.assertEqual([sup, createrole, createdb, login, repl, bypass, no_password],
                         [False, False, False, False, False, False, True])
        self.assertEqual([m for m in self.after["members"] if "products_reader" in m], [],
                         "products_reader must have no membership in either direction")
        self.assertEqual(self.c.login_roles(), self.logins_before, "a login role was introduced")

    def test_reader_selects_every_business_table_and_nothing_else_in_the_schema(self):
        self.assertTrue(self.c.role_exists("products_reader"), "products_reader does not exist")
        tables = self.c.js("SELECT json_agg(tablename ORDER BY tablename) FROM pg_tables WHERE schemaname = 'products_schema'")
        self.assertEqual(sorted(tables), sorted(BUSINESS_TABLES + [LEDGER]),
                         "a table outside the seven reviewed ones needs a new classification before any grant")
        self.assertEqual(self.c.one("SELECT has_schema_privilege('products_reader', 'products_schema', 'USAGE')"), "t")
        self.assertEqual(self.c.one("SELECT has_schema_privilege('products_reader', 'products_schema', 'CREATE')"), "f")
        for table in BUSINESS_TABLES:
            granted = self.c.js(f"""SELECT json_agg(p ORDER BY p) FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE',
                'TRUNCATE','REFERENCES','TRIGGER']) p WHERE has_table_privilege('products_reader', 'products_schema.{table}', p)""")
            self.assertEqual(granted, ["SELECT"], f"products_reader privileges on {table}")
            self.assertEqual(self.c.sql(f"SET ROLE products_reader; SELECT count(*) FROM products_schema.{table};").rc, 0)

    def test_reader_cannot_write_any_business_table(self):
        self.assertTrue(self.c.role_exists("products_reader"), "products_reader does not exist")
        prefix = "BEGIN;\n" + POPULATE
        for table, statements in WRITES.items():
            for action, statement in zip(("INSERT", "UPDATE", "DELETE"), statements):
                with self.subTest(table=table, action=action):
                    # control: the very same statement is valid for a privileged role
                    self.assertEqual(self.c.sql(prefix + statement + ";\nROLLBACK;").rc, 0,
                                     f"control failed, the statement is not valid: {statement}")
                    self.expect_denied(self.c, "table", table,
                                       prefix + "SET LOCAL ROLE products_reader;\n" + statement + ";\nROLLBACK;")

    def test_reader_has_no_ledger_and_no_other_domain_business_access(self):
        self.assertTrue(self.c.role_exists("products_reader"), "products_reader does not exist")
        self.assertEqual(self.c.one(f"SELECT has_table_privilege('products_reader', 'products_schema.{LEDGER}', 'SELECT')"), "f")
        self.expect_denied(self.c, "table", LEDGER, f"SET ROLE products_reader; SELECT 1 FROM products_schema.{LEDGER};")
        for schema, table in OTHER_DOMAINS.items():
            with self.subTest(schema=schema):
                self.assertEqual(self.c.one(f"SELECT has_schema_privilege('products_reader', '{schema}', 'USAGE')"), "f")
                self.assertEqual(self.c.one(f"SELECT has_table_privilege('products_reader', '{schema}.{table}', 'SELECT')"), "f")
                self.expect_denied(self.c, "schema", schema, f"SET ROLE products_reader; SELECT 1 FROM {schema}.{table};")

    def test_default_privileges_follow_the_effective_creator_not_postgres(self):
        entries = [d for d in self.after["default_acl"] if d[1] == "r"]
        creators = {d[0] for d in entries}
        self.assertIn(MIGRATOR, creators, "no default privileges recorded for the effective migration creator")
        reader_grants = {role: [a for a in acl.strip("{}").split(",") if a.startswith("products_reader=")]
                         for role, _, acl in entries}
        self.assertEqual(reader_grants.get(MIGRATOR), [f"products_reader=r/{MIGRATOR}"])
        self.assertEqual(reader_grants.get("postgres", []), [], "default privileges were hardcoded to postgres")

        probe = "pro15_probe_future"
        self.c.sql_ok(f"CREATE TABLE products_schema.{probe} (id int PRIMARY KEY, v text);"
                      f"INSERT INTO products_schema.{probe} VALUES (1, 'x');", user=MIGRATOR)
        self.addCleanup(self.c.sql_ok, f"DROP TABLE IF EXISTS products_schema.{probe};", MIGRATOR)
        self.assertEqual(self.c.one(f"SELECT tableowner FROM pg_tables WHERE tablename = '{probe}'"), MIGRATOR)
        self.assertEqual(self.c.sql(f"SET ROLE products_reader; SELECT count(*) FROM products_schema.{probe};").rc, 0,
                         "a future table of the effective creator must be readable")
        for statement in (f"INSERT INTO products_schema.{probe} VALUES (2, 'y')",
                          f"UPDATE products_schema.{probe} SET v = v",
                          f"DELETE FROM products_schema.{probe}"):
            self.expect_denied(self.c, "table", probe, f"SET ROLE products_reader; {statement};")

    def test_second_migrate_is_a_noop_and_changes_nothing(self):
        rows, structure = self.c.rows(), self.c.structure()
        res = self.c.migrate()
        self.assertEqual(res.rc, 0, res.out[-600:])
        self.assertIn("No migration necessary", res.out)
        self.assertEqual(self.c.rows(), rows)
        self.assertEqual(self.c.structure(), structure)

    def test_reexecuting_the_new_migrations_raw_is_safe(self):
        before = self.c.structure()
        for folder in ("01_ddl/04_alter", "03_dcl/00_roles", "03_dcl/01_grants"):
            path = self.new_migration(folder)
            for attempt in (1, 2):
                res = self.c.run_file(path)
                self.assertEqual(res.rc, 0, f"{path.name} (run {attempt}) is not re-executable: {res.err[-400:]}")
        self.assertEqual(self.c.structure(), before)


# ---------------------------------------------- populated upgrade, no-ops

class PopulatedUpgrade(IntegrityCase):
    def test_upgrade_from_populated_v017_preserves_rows_and_frozen_values(self):
        c = self.cluster()
        c.baseline()
        c.sql_ok(POPULATE)
        rows_before, structure_before = c.rows(), c.structure()
        prices_before = c.one("SELECT string_agg(unit_price_cents::text, ',' ORDER BY line_id) FROM products_schema.stock_reservation_line")
        self.assertEqual(prices_before, "4999,7999,4999")

        self.assertMigratedToLatest(c, c.migrate())

        self.assertEqual(c.rows(), rows_before, "an upgrade must not change a single row")
        self.assertEqual(c.one("SELECT string_agg(unit_price_cents::text, ',' ORDER BY line_id) FROM products_schema.stock_reservation_line"),
                         prices_before)
        self.assertEqual(c.one(f"SELECT price_cents FROM products_schema.product WHERE product_id = '{IDS['p1']}'"), "5999")
        self.assertNamedUniqueConstraint(c)
        self.assertTrue(c.role_exists("products_reader"))

        structure_latest = c.structure()
        again = c.migrate()
        self.assertIn("No migration necessary", again.out)
        self.assertEqual((c.rows(), c.structure()), (rows_before, structure_latest))
        self.assertNotEqual(structure_latest, structure_before)


class DirtyLegacyData(IntegrityCase):
    def test_duplicates_abort_the_migration_before_any_change_and_are_never_cleaned(self):
        c = self.cluster()
        c.baseline()
        c.sql_ok(POPULATE + DUPLICATE_LINE)
        rows, structure = c.rows(), c.structure()
        count = c.one(f"SELECT count(*) FROM products_schema.{LINE_TABLE}")
        self.assertEqual(count, "4")

        res = c.migrate()

        self.assertNotEqual(res.rc, 0, "a database holding duplicate (reservation_id, product_id) pairs must NOT migrate")
        output = (res.out + res.err).lower()
        self.assertIn("duplicate", output)
        self.assertIn(LINE_TABLE, output)
        for value in (IDS["r1"], IDS["p1"], IDS["l4"]):
            self.assertNotIn(value, output, "the abort message must be fixed text, not row data")
        # nothing was mutated and nothing was repaired for the migration
        self.assertEqual(c.rows(), rows)
        self.assertEqual(c.one(f"SELECT count(*) FROM products_schema.{LINE_TABLE}"), count)
        self.assertEqual(c.structure()["constraints"], structure["constraints"])
        self.assertEqual(c.structure()["invalid_indexes"], 0, "no partial index may be left behind")
        self.assertEqual(c.one(f"SELECT count(*) FROM pg_constraint WHERE conname = '{CONSTRAINT}'"), "0")
        self.assertFalse(c.role_exists("products_reader"), "the role migration must not run after the abort")
        self.assertEqual(c.one(f"SELECT coalesce(max(version::int), {BASELINE}) FROM products_schema.{LEDGER} WHERE success"),
                         str(BASELINE))
        # The disposable fixture is destroyed as it is, evidence only: it is
        # deliberately not repaired and never promoted.


class InvalidPartialIndex(IntegrityCase):
    def invalid_index_cluster(self):
        """V017 + a failed CREATE UNIQUE INDEX CONCURRENTLY, i.e. the leftover an
        interrupted non-transactional build produces. The duplicates that made
        it fail are then removed so the invalid index is the only obstacle."""
        c = self.cluster()
        c.baseline()
        c.sql_ok(POPULATE + DUPLICATE_LINE)
        build = c.sql(f"CREATE UNIQUE INDEX CONCURRENTLY {CONSTRAINT} ON products_schema.{LINE_TABLE} (reservation_id, product_id);",
                      user=MIGRATOR)
        if build.rc == 0:
            raise HarnessError("the concurrent build was expected to fail on the duplicate")
        c.sql_ok(f"DELETE FROM products_schema.{LINE_TABLE} WHERE line_id = '{IDS['l4']}';")
        if c.structure()["invalid_indexes"] != 1:
            raise HarnessError("fixture did not leave exactly one invalid index")
        return c

    def test_raw_v018_fails_with_guidance_on_an_invalid_partial_index(self):
        c = self.invalid_index_cluster()
        rows, structure = c.rows(), c.structure()

        res = c.run_file(self.new_migration("01_ddl/04_alter"))

        self.assertNotEqual(res.rc, 0, "an invalid partial index must not be silently accepted or repaired")
        message = res.err.lower()
        self.assertIn("invalid", message)
        self.assertIn(CONSTRAINT, message)
        self.assertEqual(c.one(f"SELECT count(*) FROM pg_constraint WHERE conname = '{CONSTRAINT}'"), "0")
        self.assertEqual((c.rows(), c.structure()), (rows, structure), "the failed attempt changed the database")

    def test_u018_removes_a_partial_owned_index_without_touching_rows(self):
        rollback = next((p for p in rollback_scripts() if rollback_number(p) == BASELINE + 1), None)
        self.assertIsNotNone(rollback, f"U{BASELINE + 1:03d} does not exist")
        c = self.invalid_index_cluster()
        rows = c.rows()
        clean = self.cluster()  # expected structure only: same baseline, no leftovers
        clean.baseline()
        clean.sql_ok(POPULATE)

        res = c.run_file(rollback)

        self.assertEqual(res.rc, 0, res.err[-400:])
        self.assertEqual(c.structure(), clean.structure(), "U018 must leave exactly the V017 structure")
        self.assertEqual(c.rows(), rows)


# ------------------------------------------- unsafe / safe existing role

class ExistingReaderRole(IntegrityCase):
    def test_an_unsafe_same_name_role_is_refused_and_granted_nothing(self):
        c = self.cluster()
        c.baseline()
        c.sql_ok(f"CREATE ROLE products_reader LOGIN CREATEDB PASSWORD '{secrets.token_hex(8)}';")

        res = c.migrate()

        self.assertGreater(len(repo_versions()), BASELINE, f"no migration after V{BASELINE:03d} exists in the repository")
        self.assertNotEqual(res.rc, 0, "an existing LOGIN products_reader must not be silently accepted")
        self.assertEqual(c.one("SELECT has_schema_privilege('products_reader', 'products_schema', 'USAGE')"), "f")
        for table in BUSINESS_TABLES:
            self.assertEqual(c.one(f"SELECT has_table_privilege('products_reader', 'products_schema.{table}', 'SELECT')"), "f")
        self.assertEqual([d for d in c.structure()["default_acl"] if "products_reader" in d[2]], [])

    def test_preexisting_write_or_foreign_access_is_refused_before_new_grants(self):
        for access in ("write", "foreign"):
            with self.subTest(access=access):
                c = self.cluster()
                c.baseline()
                c.sql_ok("CREATE ROLE products_reader NOLOGIN;")
                if access == "write":
                    c.sql_ok("GRANT UPDATE ON products_schema.product TO products_reader;")
                else:
                    c.sql_ok("GRANT USAGE ON SCHEMA customers_schema TO products_reader;"
                             "GRANT SELECT ON customers_schema.customer TO products_reader;")
                rows, before = c.rows(), c.structure()

                migrated = c.migrate()

                self.assertNotEqual(migrated.rc, 0,
                                    "unsafe existing permissions must stop adoption, not gain additional access")
                self.assertEqual(c.rows(), rows)
                self.assertEqual(c.structure()["table_acl"], before["table_acl"])
                self.assertEqual(c.one("SELECT has_table_privilege('products_reader', 'products_schema.product', 'SELECT')"), "f")

    def test_a_safe_existing_role_is_adopted_idempotently(self):
        c = self.cluster()
        c.baseline()
        c.sql_ok("CREATE ROLE products_reader NOLOGIN;")

        self.assertMigratedToLatest(c, c.migrate())

        self.assertEqual(c.one("SELECT has_table_privilege('products_reader', 'products_schema.product', 'SELECT')"), "t")
        self.assertEqual(c.one("SELECT rolcanlogin::text || rolsuper::text || (rolpassword IS NULL)::text FROM pg_authid WHERE rolname = 'products_reader'"),
                         "falsefalsetrue")


# ----------------------------------------------------------- rollbacks

class Rollbacks(IntegrityCase):
    def new_rollbacks(self):
        scripts = [p for p in rollback_scripts() if rollback_number(p) > BASELINE]
        versions = sorted(v for v in repo_versions() if v > BASELINE)
        self.assertGreater(len(versions), 0, f"no migration after V{BASELINE:03d} exists in the repository")
        self.assertEqual(sorted(rollback_number(p) for p in scripts), versions,
                         "every new V migration needs exactly one mirrored U rollback")
        for path in scripts:
            self.assertRegex(path.name, r"^U\d{3}__.+\.rollback\.sql$")
        return scripts  # highest first, as the CI discovery orders them

    def apply_all(self, cluster, scripts):
        applied = []
        for path in scripts:
            res = cluster.run_file(path)
            self.assertEqual(res.rc, 0, f"{path.name} failed: {res.err[-400:]}")
            applied.append(path.name)
        return applied

    def test_new_rollbacks_20_to_18_then_reapply_restores_everything(self):
        c = self.cluster()
        self.populate_then_latest(c)
        probe = "pro15_probe_rollback"
        c.sql_ok(f"CREATE TABLE products_schema.{probe} (id int PRIMARY KEY);", user=MIGRATOR)
        latest_rows = c.rows()
        scripts = self.new_rollbacks()
        v18_to_20 = [p.name for p in scripts]
        self.assertEqual(v18_to_20, sorted(v18_to_20, reverse=True), "rollbacks must be applied highest version first")

        self.assertEqual(self.apply_all(c, scripts), v18_to_20)

        self.assertEqual(c.one(f"SELECT count(*) FROM pg_constraint WHERE conname = '{CONSTRAINT}'"), "0")
        self.assertEqual(c.one(f"SELECT count(*) FROM pg_indexes WHERE indexname = '{CONSTRAINT}'"), "0")
        self.assertFalse(c.role_exists("products_reader"), "U019 must drop the role (U020 revokes first)")
        self.assertNotIn("products_reader", c.one(f"SELECT coalesce(relacl::text, '') FROM pg_class WHERE relname = '{probe}'"))
        self.assertEqual(c.rows(), latest_rows, "a rollback must not touch rows")
        c.sql_ok(f"DROP TABLE products_schema.{probe};", user=MIGRATOR)
        rolled_back = c.structure()
        self.assertEqual([d for d in rolled_back["default_acl"] if "products_reader" in d[2]], [])
        self.assertEqual(rolled_back["invalid_indexes"], 0)

        # TEST-ONLY bookkeeping on a disposable cluster: forget the ledger rows of
        # the versions just rolled back so Flyway may apply them again. This is
        # not a procedure for persistent environments.
        c.sql_ok(f"DELETE FROM products_schema.{LEDGER} WHERE version IS NOT NULL AND version::int > {BASELINE};")
        res = c.migrate()
        self.assertMigratedToLatest(c, res)
        self.assertEqual(c.rows(), latest_rows, "reapplying must preserve the original data")
        self.assertNamedUniqueConstraint(c)
        self.assertTrue(c.role_exists("products_reader"))
        self.assertNotEqual(c.structure(), rolled_back)

    def test_full_historical_rollback_discovery_order_and_rebuild(self):
        c = self.cluster()
        c.baseline()
        res = c.migrate()
        self.assertMigratedToLatest(c, res)
        structure = c.structure()

        discovered = rollback_scripts()
        names = [p.name for p in discovered]
        self.assertEqual(sorted(rollback_number(p) for p in discovered),
                         sorted(repo_versions()), "every V migration, historical and new, has one U script")
        self.assertEqual(names, sorted(names, reverse=True), "sort -r order is highest version first")
        new = [n for n in names if int(re.match(r"U(\d+)", n).group(1)) > BASELINE]
        self.assertGreater(len(new), 0, "no new rollback was discovered")
        self.assertEqual(names[:len(new)], new, "new rollbacks must run before every historical one")

        # as db-ci.yml does: the ledger goes first, then every U script
        c.sql_ok(f"DROP TABLE IF EXISTS products_schema.{LEDGER};")
        self.assertEqual(self.apply_all(c, discovered), names)

        self.assertEqual(c.one("SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'products_schema'"), "0")
        for role in ("products_writer", "products_reader"):
            self.assertFalse(c.role_exists(role), f"{role} survived the full rollback")

        rebuilt = c.migrate()
        self.assertMigratedToLatest(c, rebuilt)
        self.assertEqual(c.structure(), structure, "a full rebuild must reproduce the same structure")


if __name__ == "__main__":
    if os.environ.get("PRODUCTS_TEST_DISPOSABLE") != "1":
        sys.exit("refusing to run: set PRODUCTS_TEST_DISPOSABLE=1 (this harness creates and destroys Docker containers)")
    unittest.main(verbosity=2)

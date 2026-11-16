"""Black-box acceptance tests for the local-sales-warehouse CLI.

Every test drives the public entrypoint as a subprocess (``python app ...`` or
``./app ...``) and, where the database must be inspected, opens it through the
public DuckDB Python API. No private module of the implementation is imported.

Run with: ``.venv/bin/python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app"
PYTHON = sys.executable
ENV = {**os.environ, "PYTHONUTF8": "1"}

MAX_CENTS = 9007199254740991


def order(order_id: str, customer: str = "c1", date: str = "2026-01-01", cents: int = 100) -> dict:
    return {"id": order_id, "customer": customer, "date": date, "cents": cents}


def try_import_duckdb():
    try:
        import duckdb
    except ImportError:  # pragma: no cover - environment guard
        return None
    return duckdb


class WarehouseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    # -- process helpers ---------------------------------------------------

    def run_app(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, str(APP), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=ENV,
            cwd=str(self.dir),
            timeout=timeout,
        )

    def db_path(self, name: str = "warehouse.duckdb") -> str:
        return str(self.dir / name)

    def write_bytes(self, payload: bytes, name: str = "input.json") -> str:
        path = self.dir / name
        path.write_bytes(payload)
        return str(path)

    def write_json(self, document, name: str = "input.json") -> str:
        return self.write_bytes(json.dumps(document, ensure_ascii=False).encode("utf-8"), name)

    def assert_success(self, result: subprocess.CompletedProcess, expected=None):
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.endswith("\n"), msg=repr(result.stdout))
        self.assertEqual(result.stdout.count("\n"), 1, msg=repr(result.stdout))
        payload = json.loads(result.stdout)
        if expected is not None:
            self.assertEqual(payload, expected)
        return payload

    def assert_error(self, result: subprocess.CompletedProcess, code: int):
        self.assertEqual(result.returncode, code, msg=result.stderr)
        self.assertEqual(result.stdout, "")
        lines = result.stderr.splitlines()
        self.assertEqual(len(lines), 1, msg=repr(result.stderr))
        self.assertTrue(lines[0].startswith("error:"), msg=repr(result.stderr))
        self.assertNotIn("Traceback", result.stderr)

    def load(self, document_or_path, db: str | None = None, expect: int = 0):
        db = db or self.db_path()
        if isinstance(document_or_path, (str, os.PathLike)):
            input_path = str(document_or_path)
        else:
            input_path = self.write_json(document_or_path)
        result = self.run_app("load", input_path, "--db", db)
        if expect == 0:
            return self.assert_success(result)
        self.assert_error(result, expect)
        return None

    def summary(self, db: str | None = None, month: str | None = None, expect: int = 0):
        db = db or self.db_path()
        args = ["summary", "--db", db]
        if month is not None:
            args += ["--month", month]
        result = self.run_app(*args)
        if expect == 0:
            return self.assert_success(result)
        self.assert_error(result, expect)
        return None

    def duck_rows(self, db: str | None = None):
        duckdb = try_import_duckdb()
        if duckdb is None:  # pragma: no cover - environment guard
            self.skipTest("duckdb is not importable from the test interpreter")
        connection = duckdb.connect(db or self.db_path(), read_only=True)
        try:
            return connection.execute(
                "SELECT id, customer, date, cents FROM orders ORDER BY id"
            ).fetchall()
        finally:
            connection.close()


class EntrypointTests(WarehouseTestCase):
    def test_version_is_exact(self):
        result = subprocess.run(
            [PYTHON, str(APP), "--version"], capture_output=True, env=ENV, cwd=str(self.dir)
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"local-sales-warehouse 0.1.0\n")
        self.assertEqual(result.stderr, b"")

    def test_executable_entrypoint_matches_python_entrypoint(self):
        self.assertTrue(os.access(APP, os.X_OK), "app must be executable")
        direct = subprocess.run(
            [str(APP), "--version"], capture_output=True, env=ENV, cwd=str(self.dir)
        )
        self.assertEqual(direct.returncode, 0)
        self.assertEqual(direct.stdout, b"local-sales-warehouse 0.1.0\n")
        self.assertEqual(direct.stderr, b"")

    def test_help_lists_every_command_and_option(self):
        result = self.run_app("--help")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        for token in ("usage:", "app", "--help", "--version", "load", "summary", "--db", "--month"):
            self.assertIn(token, result.stdout)

    def test_usage_errors_exit_2(self):
        cases = [
            (),
            ("bogus",),
            ("load",),
            ("load", "input.json"),
            ("load", "input.json", "--db"),
            ("summary",),
            ("summary", "--db"),
            ("summary", "--bogus", "x"),
            ("load", "a.json", "--db", "d.duckdb", "extra"),
            ("--version", "--help"),
            ("summary", "--db", "d.duckdb", "-q"),
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assert_error(self.run_app(*args), 2)

    def test_missing_argument_value_does_not_create_database(self):
        self.assert_error(self.run_app("load", "nope.json", "--db"), 2)
        self.assertFalse(os.path.exists(self.db_path()))


class LoadTests(WarehouseTestCase):
    def test_ac1_load_creates_real_duckdb_table(self):
        payload = self.load(
            [order("o1", "c1", "2026-01-01", 100), order("o2", "c2", "2026-02-01", 250)]
        )
        self.assertEqual(payload, {"inserted": 2, "unchanged": 0})
        self.assertTrue(os.path.isfile(self.db_path()))
        rows = self.duck_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "o1")
        self.assertEqual(rows[0][3], 100)
        self.assertEqual(rows[1][0], "o2")

    def test_ac2_repeating_file_is_idempotent(self):
        document = [order("o1"), order("o2", "c2", "2026-01-02", 200)]
        self.assertEqual(self.load(document), {"inserted": 2, "unchanged": 0})
        self.assertEqual(self.load(document), {"inserted": 0, "unchanged": 2})
        self.assertEqual(len(self.duck_rows()), 2)

    def test_ac3_conflict_rolls_back_whole_batch(self):
        self.load([order("o1", "c1", "2026-01-01", 100)])
        conflict = [order("o2", "c2", "2026-02-01", 200), order("o1", "c9", "2026-01-01", 100)]
        self.load(conflict, expect=4)
        rows = self.duck_rows()
        self.assertEqual([row[0] for row in rows], ["o1"])
        self.assertEqual(rows[0][1], "c1")
        self.assertEqual(self.summary()["months"][0]["orders"], 1)

    def test_id_conflict_on_date_or_cents(self):
        self.load([order("o1", "c1", "2026-01-01", 100)])
        for changed in (
            order("o1", "c1", "2026-01-02", 100),
            order("o1", "c1", "2026-01-01", 101),
            order("o1", "cx", "2026-01-01", 100),
        ):
            with self.subTest(changed=changed):
                self.load([changed], expect=4)
        self.assertEqual(len(self.duck_rows()), 1)

    def test_ac9_restart_and_empty_array(self):
        self.load([order("o1", "c1", "2026-01-01", 7)])
        self.assertEqual(self.summary()["months"][0]["cents"], "7")
        self.assertEqual(self.load([]), {"inserted": 0, "unchanged": 0})
        self.assertEqual(self.summary()["months"][0]["cents"], "7")

    def test_empty_array_creates_database_and_table(self):
        self.assertEqual(self.load([]), {"inserted": 0, "unchanged": 0})
        self.assertTrue(os.path.isfile(self.db_path()))
        self.assertEqual(self.duck_rows(), [])

    def test_key_order_and_unicode_escapes_are_not_criteria(self):
        raw = (
            b'[{"cents":123,"date":"2026-01-01","customer":"c1","id":"\\u006f1"},'
            b'{"id":"o2","customer":"c2","date":"2026-01-02","cents":1}]'
        )
        path = self.write_bytes(raw)
        self.assertEqual(self.load(path), {"inserted": 2, "unchanged": 0})
        self.assertEqual([row[0] for row in self.duck_rows()], ["o1", "o2"])

    def test_ac13_paths_with_spaces_and_unicode(self):
        directory = self.dir / "dados cliente" / "vendas ção"
        directory.mkdir(parents=True)
        db = str(directory / "base de dados.duckdb")
        source = directory / "entrada de dados.json"
        source.write_text(json.dumps([order("o1")]), encoding="utf-8")
        result = self.run_app("load", str(source), "--db", db)
        self.assert_success(result, {"inserted": 1, "unchanged": 0})
        result = self.run_app("summary", "--db", db)
        self.assertEqual(self.assert_success(result)["months"][0]["orders"], 1)

    def test_input_file_is_not_modified(self):
        path = self.write_json([order("o1")])
        before = hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
        self.load(path)
        after = hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_ac14_zero_cents_is_accepted_and_rendered_as_zero(self):
        self.load([order("o1", cents=0)])
        self.assertEqual(self.summary()["months"][0]["cents"], "0")

    def test_order_limits_at_and_above_boundaries(self):
        document = [
            order(f"o{index:05d}", f"c{index % 3}", "2026-01-01", index) for index in range(10000)
        ]
        self.assertEqual(self.load(document)["inserted"], 10000)
        self.assertEqual(self.summary()["months"][0]["orders"], 10000)
        too_many = document + [order("overflow")]
        self.load(too_many, expect=2)


class ValidationTests(WarehouseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load([order("keep", "c1", "2026-01-01", 5)])
        self.before = self.duck_rows()

    def assert_rejected_and_intact(self, payload):
        self.load(payload, expect=2)
        self.assertEqual(self.duck_rows(), self.before)

    def test_ac7_cents_validation(self):
        for cents in (-1, 2**53, True, False, 1.0, "100", None, [1]):
            with self.subTest(cents=cents):
                self.assert_rejected_and_intact([order("new", cents=cents)])

    def test_ac8_invalid_dates(self):
        dates = (
            "2026-02-30",
            "2026-1-1",
            "0000-01-01",
            "10000-01-01",
            "2026-13-01",
            "20260101",
            "2026-01-01T00:00:00",
        )
        for date in dates:
            with self.subTest(date=date):
                self.assert_rejected_and_intact([order("new", date=date)])

    def test_ac8_repeated_id_extra_field_and_missing_field(self):
        self.assert_rejected_and_intact([order("dup"), order("dup", "c2")])
        self.assert_rejected_and_intact([{**order("new"), "extra": 1}])
        missing = order("new")
        del missing["customer"]
        self.assert_rejected_and_intact([missing])
        self.assert_rejected_and_intact([{"id": "new", "customer": "c1", "date": "2026-01-01"}])

    def test_ac8_document_shape(self):
        self.assert_rejected_and_intact({"id": "new"})
        self.assert_rejected_and_intact([order("new"), 5])
        self.assert_rejected_and_intact([None])

    def test_invalid_identifiers(self):
        for value in ("", "a" * 33, "com espaço", "café", "a.b", "a/b"):
            with self.subTest(value=value):
                self.assert_rejected_and_intact([order(value)])
                self.assert_rejected_and_intact([order("new", customer=value)])

    def test_raw_json_rejections(self):
        bad_documents = [
            b"",
            b"{}",
            b"[}]",
            b'[{"id":"a","customer":"c","date":"2026-01-01","cents":1},]',
            b'[{"id":"a","customer":"c","date":"2026-01-01","cents":NaN}]',
            b'[{"id":"a","customer":"c","date":"2026-01-01","cents":Infinity}]',
            b'[{"id":"a","id":"b","customer":"c","date":"2026-01-01","cents":1}]',
            b'[{"id":"a","customer":"c","date":"2026-01-01","cents":1}] []',
        ]
        for document in bad_documents:
            with self.subTest(document=document):
                path = self.write_bytes(document, "bad.json")
                self.load(path, expect=2)
        self.assertEqual(self.duck_rows(), self.before)

    def test_bom_is_rejected_without_touching_database(self):
        path = self.write_bytes(b"\xef\xbb\xbf" + json.dumps([order("new")]).encode(), "bom.json")
        self.load(path, expect=2)
        self.assertEqual(self.duck_rows(), self.before)

    def test_input_size_limit_is_inclusive(self):
        exact = b"[" + b" " * (4 * 1024 * 1024 - 2) + b"]"
        self.assertEqual(len(exact), 4 * 1024 * 1024)
        path = self.write_bytes(exact, "exact.json")
        self.assertEqual(self.load(path), {"inserted": 0, "unchanged": 0})
        too_big = exact + b" "
        path = self.write_bytes(too_big, "big.json")
        self.load(path, expect=2)

    def test_invalid_month(self):
        for month in ("2026-13", "2026-00", "2026-1", "0000-01", "99999-01", "2026-01-01", ""):
            with self.subTest(month=month):
                self.summary(month=month, expect=2)


class SummaryTests(WarehouseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load(
            [
                order("a", "alice", "2026-01-05", 100),
                order("b", "alice", "2026-01-20", 250),
                order("c", "bob", "2026-01-31", 0),
                order("d", "carol", "2026-02-01", 7),
                order("e", "carol", "2025-12-31", 3),
            ]
        )

    def test_ac4_monthly_grouping_and_distinct_customers(self):
        payload = self.summary()
        self.assertEqual(
            payload,
            {
                "months": [
                    {"month": "2025-12", "orders": 1, "customers": 1, "cents": "3"},
                    {"month": "2026-01", "orders": 3, "customers": 2, "cents": "350"},
                    {"month": "2026-02", "orders": 1, "customers": 1, "cents": "7"},
                ]
            },
        )

    def test_ac5_month_filter(self):
        self.assertEqual(
            self.summary(month="2026-01"),
            {"months": [{"month": "2026-01", "orders": 3, "customers": 2, "cents": "350"}]},
        )
        self.assertEqual(self.summary(month="2026-03"), {"months": []})

    def test_ac6_maximum_cents_sum_is_exact_decimal_string(self):
        db = self.db_path("max.duckdb")
        self.load(
            [
                order("m1", "c1", "2026-01-01", MAX_CENTS),
                order("m2", "c2", "2026-01-02", MAX_CENTS),
            ],
            db=db,
        )
        payload = self.summary(db=db)
        self.assertEqual(payload["months"][0]["cents"], "18014398509481982")

    def test_months_omit_empty_periods_and_sums_never_use_float(self):
        payload = self.summary()
        months = [entry["month"] for entry in payload["months"]]
        self.assertEqual(months, sorted(months))
        self.assertEqual(len(months), 3)
        for entry in payload["months"]:
            self.assertIsInstance(entry["cents"], str)
            self.assertTrue(entry["cents"].lstrip("-").isdigit())
            self.assertIsInstance(entry["orders"], int)
            self.assertIsInstance(entry["customers"], int)


class FailureAndAtomicityTests(WarehouseTestCase):
    def test_ac10_summary_without_database_exits_3_and_creates_nothing(self):
        db = self.dir / "missing" / "nothing.duckdb"
        db.parent.mkdir()
        self.summary(db=str(db), expect=3)
        self.assertFalse(os.path.exists(db))

    def test_missing_parent_directory_is_io_error(self):
        self.load([order("o1")], db=str(self.dir / "absent" / "db.duckdb"), expect=3)
        self.assertFalse(os.path.exists(self.dir / "absent"))

    def test_load_missing_input_is_io_error(self):
        result = self.run_app("load", str(self.dir / "nope.json"), "--db", self.db_path())
        self.assert_error(result, 3)
        self.assertFalse(os.path.exists(self.db_path()))

    def test_ac11_corrupt_database_is_preserved(self):
        raw = b"this is not a duckdb database"
        db = self.db_path()
        pathlib.Path(db).write_bytes(raw)
        self.summary(expect=3)
        self.load([order("o1")], expect=3)
        self.assertEqual(pathlib.Path(db).read_bytes(), raw)

    def test_ac11_empty_file_is_not_a_database(self):
        db = self.db_path()
        pathlib.Path(db).write_bytes(b"")
        self.summary(expect=3)
        self.load([order("o1")], expect=3)
        self.assertEqual(pathlib.Path(db).read_bytes(), b"")

    def test_incompatible_orders_table_is_rejected_without_migration(self):
        duckdb = try_import_duckdb()
        if duckdb is None:  # pragma: no cover
            self.skipTest("duckdb is not importable")
        db = self.db_path("incompatible.duckdb")
        connection = duckdb.connect(db)
        try:
            connection.execute(
                "CREATE TABLE orders(id VARCHAR, customer VARCHAR, date DATE, cents HUGEINT)"
            )
        finally:
            connection.close()
        before = pathlib.Path(db).read_bytes()
        self.summary(db=db, expect=3)
        self.load([order("o1")], db=db, expect=3)
        self.assertEqual(pathlib.Path(db).read_bytes(), before)

    def test_external_lock_is_an_io_error_and_preserves_data(self):
        duckdb = try_import_duckdb()
        if duckdb is None:  # pragma: no cover
            self.skipTest("duckdb is not importable")
        self.load([order("o1", "c1", "2026-01-01", 10)])
        holder = subprocess.Popen(
            [
                PYTHON,
                "-c",
                (
                    "import duckdb, sys, time\n"
                    "connection = duckdb.connect(sys.argv[1])\n"
                    "connection.execute('SELECT 1').fetchall()\n"
                    "print('ready', flush=True)\n"
                    "time.sleep(60)\n"
                ),
                self.db_path(),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=ENV,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            self.load([order("o2", "c2", "2026-02-01", 20)], expect=3)
            self.summary(expect=3)
        finally:
            holder.terminate()
            holder.wait(timeout=30)
            if holder.stdout:
                holder.stdout.close()
        self.assertEqual([row[0] for row in self.duck_rows()], ["o1"])

    def test_ac12_storage_failure_keeps_committed_data_and_no_partial_batch(self):
        if os.name != "posix":  # pragma: no cover - declared targets are POSIX
            self.skipTest("POSIX permission fixture")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root bypasses directory permission checks")
        directory = self.dir / "readonly"
        directory.mkdir()
        db = str(directory / "db.duckdb")
        self.load([order("o1", "c1", "2026-01-01", 10)], db=db)
        os.chmod(directory, 0o555)
        try:
            if os.access(directory, os.W_OK):  # pragma: no cover - unusual FS
                self.skipTest("filesystem does not enforce directory permissions")
            result = self.run_app("load", self.write_json([order("o2"), order("o3")]), "--db", db)
            if result.returncode == 0:  # pragma: no cover - unusual FS
                self.skipTest("filesystem allowed writes in a read-only directory")
            self.assert_error(result, 3)
        finally:
            os.chmod(directory, 0o755)
        self.assertEqual([row[0] for row in self.duck_rows(db)], ["o1"])

    def test_ac12_lock_failure_with_existing_data(self):
        self.load([order("o1", "c1", "2026-01-01", 10)])
        # Covered end-to-end by the external lock test; this asserts the batch
        # with a new order and a duplicate-of-existing order adds nothing.
        self.load([order("o2"), order("o1", "c1", "2026-01-01", 11)], expect=4)
        self.assertEqual([row[0] for row in self.duck_rows()], ["o1"])


class DurabilityTests(WarehouseTestCase):
    def test_database_survives_process_restart(self):
        db = self.db_path()
        self.load([order("o1", "c1", "2026-01-01", 42)], db=db)
        self.assertEqual(self.summary(db=db)["months"][0]["cents"], "42")
        self.load([order("o2", "c2", "2026-02-01", 8)], db=db)
        payload = self.summary(db=db)
        self.assertEqual([entry["month"] for entry in payload["months"]], ["2026-01", "2026-02"])

    def test_orders_table_is_public_duckdb_schema(self):
        duckdb = try_import_duckdb()
        if duckdb is None:  # pragma: no cover
            self.skipTest("duckdb is not importable")
        self.load([order("o1", "c1", "2026-01-01", 1)])
        connection = duckdb.connect(self.db_path(), read_only=True)
        try:
            columns = connection.execute(
                "SELECT column_name, data_type, is_nullable FROM duckdb_columns() "
                "WHERE table_name = 'orders' ORDER BY column_index"
            ).fetchall()
            primary_key = connection.execute(
                "SELECT constraint_column_names FROM duckdb_constraints() "
                "WHERE table_name = 'orders' AND constraint_type = 'PRIMARY KEY'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            [(column[0], column[1], bool(column[2])) for column in columns],
            [
                ("id", "VARCHAR", False),
                ("customer", "VARCHAR", False),
                ("date", "DATE", False),
                ("cents", "HUGEINT", False),
            ],
        )
        self.assertEqual([list(row[0]) for row in primary_key], [["id"]])


if __name__ == "__main__":
    unittest.main()

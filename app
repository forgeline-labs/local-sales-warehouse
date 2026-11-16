#!/usr/bin/env python3
"""local-sales-warehouse -- import and summarize local sales orders with DuckDB.

Public entrypoint for the local-sales-warehouse v0.1.0 CLI. See README.md and
the specification referenced there for the full contract.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import sys

PRODUCT = "local-sales-warehouse"
VERSION = "0.1.0"

MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_ORDERS_PER_FILE = 10000
MAX_ORDERS_PER_DATABASE = 10**9
MAX_CENTS = 9007199254740991

TEXT_FIELDS = ("id", "customer", "date", "cents")
ORDER_KEYS = frozenset(TEXT_FIELDS)
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})\Z")
MONTH_RE = re.compile(r"([0-9]{4})-([0-9]{2})\Z")

TABLE_COLUMNS = (
    ("id", "VARCHAR"),
    ("customer", "VARCHAR"),
    ("date", "DATE"),
    ("cents", "HUGEINT"),
)

CREATE_TABLE_SQL = (
    "CREATE TABLE orders ("
    "id VARCHAR PRIMARY KEY, "
    "customer VARCHAR NOT NULL, "
    "date DATE NOT NULL, "
    "cents HUGEINT NOT NULL)"
)

HELP_TEXT = f"""usage: app <command> [options]

{PRODUCT} {VERSION} - local sales analytics on DuckDB.

Commands:
  load INPUT --db DB
      Load a JSON array of orders from INPUT into the DuckDB database DB.
      Creates the database and the public table orders when needed.

  summary --db DB [--month YYYY-MM]
      Print per-month totals (orders, distinct customers, summed cents).
      --month restricts the result to one calendar month.

Options:
  --db DB        Path to the DuckDB database file (required by both commands).
  --month YYYY-MM
                 Month filter for summary, from 0001-01 to 9999-12.
  --help         Show this help text and exit.
  --version      Print the product version and exit.

Input:
  INPUT is a UTF-8 JSON array (at most 4 MiB) of exact objects such as
  {{"id":"o1","customer":"c1","date":"2026-01-01","cents":100}}.
  id/customer match [A-Za-z0-9_-]{{1,32}}; date is YYYY-MM-DD; cents is an
  integer from 0 to 9007199254740991.

Exit codes:
  0 success, 2 invalid input or usage, 3 I/O or database error,
  4 conflicting order id.
"""


class CliError(Exception):
    """An error that maps to a one-line stderr message and an exit code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Environment bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_venv() -> None:
    """Re-exec through the prepared .venv so ./app and `python app` agree.

    The project installs its runtime dependency into .venv via ./setup. When
    the script is started by another interpreter we hand control to the
    prepared environment, if it exists. Version/help therefore keep working
    even before setup because the import of duckdb is lazy.
    """
    if os.environ.get("LSW_IN_VENV") == "1":
        return
    root = os.path.dirname(os.path.abspath(__file__))
    venv_dir = os.path.join(root, ".venv")
    venv_python = os.path.join(venv_dir, "bin", "python")
    if not os.path.isfile(venv_python):
        return
    try:
        if os.path.abspath(sys.prefix) == os.path.abspath(venv_dir):
            return
        env = dict(os.environ)
        env["LSW_IN_VENV"] = "1"
        os.execve(venv_python, [venv_python, os.path.abspath(__file__), *sys.argv[1:]], env)
    except OSError:
        # A broken .venv should surface as a normal database/dependency error
        # rather than an exec traceback.
        return


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite numbers are not valid JSON values")


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate object key")
        obj[key] = value
    return obj


def _parse_json(text: str):
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        raise CliError(2, "invalid JSON input") from None


def _validate_date(value: str, index: int) -> _dt.date:
    match = DATE_RE.match(value)
    if match is None:
        raise CliError(2, f"order {index}: invalid date")
    year, month, day = (int(part) for part in match.groups())
    try:
        parsed = _dt.date(year, month, day)
    except ValueError:
        raise CliError(2, f"order {index}: invalid date") from None
    if parsed.isoformat() != value:
        raise CliError(2, f"order {index}: date is not an exact YYYY-MM-DD value")
    return parsed


def _validate_month(value: str) -> str:
    match = MONTH_RE.match(value)
    if match is None:
        raise CliError(2, "invalid month, expected YYYY-MM")
    year, month = int(match.group(1)), int(match.group(2))
    if not (1 <= year <= 9999) or not (1 <= month <= 12):
        raise CliError(2, "invalid month, expected YYYY-MM")
    return value


def validate_orders(document) -> list[tuple[str, str, _dt.date, int]]:
    """Validate the whole JSON document; return canonical order tuples."""
    if not isinstance(document, list):
        raise CliError(2, "input must be a JSON array")
    if len(document) > MAX_ORDERS_PER_FILE:
        raise CliError(2, "input has more than 10000 orders")

    orders: list[tuple[str, str, _dt.date, int]] = []
    seen: set[str] = set()
    for index, item in enumerate(document):
        if not isinstance(item, dict):
            raise CliError(2, f"order {index}: must be a JSON object")
        if set(item.keys()) != ORDER_KEYS:
            raise CliError(2, f"order {index}: must have exactly id, customer, date, cents")

        order_id = item["id"]
        customer = item["customer"]
        date_text = item["date"]
        cents = item["cents"]

        if not isinstance(order_id, str) or ID_RE.match(order_id) is None:
            raise CliError(2, f"order {index}: invalid id")
        if not isinstance(customer, str) or ID_RE.match(customer) is None:
            raise CliError(2, f"order {index}: invalid customer")
        if not isinstance(date_text, str):
            raise CliError(2, f"order {index}: invalid date")
        if not isinstance(cents, int) or isinstance(cents, bool):
            raise CliError(2, f"order {index}: cents must be an integer")
        if not (0 <= cents <= MAX_CENTS):
            raise CliError(2, f"order {index}: cents out of range")
        if order_id in seen:
            raise CliError(2, f"order {index}: duplicate id in input")
        seen.add(order_id)

        orders.append((order_id, customer, _validate_date(date_text, index), cents))
    return orders


def _read_input(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_INPUT_BYTES + 1)
    except OSError:
        raise CliError(3, "cannot read input file") from None
    if len(data) > MAX_INPUT_BYTES:
        raise CliError(2, "input exceeds 4 MiB")
    if data.startswith(b"\xef\xbb\xbf"):
        raise CliError(2, "input must not start with a byte order mark")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise CliError(2, "input is not valid UTF-8") from None


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------


def _duckdb():
    try:
        import duckdb
    except ImportError:
        raise CliError(3, "duckdb is not installed; run ./setup") from None
    return duckdb


def _database_parent(db_path: str) -> str:
    return os.path.dirname(os.path.abspath(db_path))


def _check_db_target(db_path: str) -> None:
    # DuckDB accepts connection identifiers that are not durable local files.
    if (
        not db_path
        or "\x00" in db_path
        or db_path.startswith(":memory:")
        or db_path == ":default:"
        or re.match(r"(?i)(?:md|motherduck|https?|s3|gs|gcs|az|azure|r2|hf):", db_path)
    ):
        raise CliError(3, "invalid database file path")
    if os.path.isdir(db_path):
        raise CliError(3, "database path is a directory")
    if not os.path.isdir(_database_parent(db_path)):
        raise CliError(3, "database parent directory does not exist")


def _connect(db_path: str, read_only: bool):
    duckdb = _duckdb()
    try:
        return duckdb.connect(os.path.abspath(db_path), read_only=read_only)
    except Exception:
        raise CliError(3, "cannot open database") from None


def _table_state(connection) -> str:
    """Classify the public orders table as missing, compatible or incompatible."""
    rows = connection.execute(
        "SELECT column_name, data_type, is_nullable "
        "FROM duckdb_columns() "
        "WHERE schema_name = 'main' AND table_name = 'orders' "
        "ORDER BY column_index"
    ).fetchall()
    if not rows:
        return "missing"
    # Column order is not part of the compatibility contract here; the names,
    # types, nullability and key constraints are.
    actual = {row[0]: (row[1], bool(row[2])) for row in rows}
    expected = {name: (data_type, False) for name, data_type in TABLE_COLUMNS}
    if actual != expected:
        return "incompatible"

    constraints = connection.execute(
        "SELECT constraint_type, constraint_column_names "
        "FROM duckdb_constraints() "
        "WHERE schema_name = 'main' AND table_name = 'orders'"
    ).fetchall()
    primary_keys = [list(columns) for kind, columns in constraints if kind == "PRIMARY KEY"]
    not_null = {tuple(columns) for kind, columns in constraints if kind == "NOT NULL"}
    if primary_keys != [["id"]]:
        return "incompatible"
    if not_null != {("id",), ("customer",), ("date",), ("cents",)}:
        return "incompatible"
    return "compatible"


def _ensure_table(connection) -> None:
    state = _table_state(connection)
    if state == "incompatible":
        raise CliError(3, "database schema is incompatible with orders")
    if state == "missing":
        connection.execute(CREATE_TABLE_SQL)


def _apply_orders(connection, orders) -> dict[str, int]:
    connection.execute(
        "CREATE TEMPORARY TABLE __lsw_batch ("
        "id VARCHAR, customer VARCHAR, date DATE, cents HUGEINT)"
    )
    if orders:
        connection.executemany(
            "INSERT INTO __lsw_batch (id, customer, date, cents) VALUES (?, ?, ?, ?)",
            orders,
        )

    unchanged = 0
    existing = connection.execute(
        "SELECT b.id, b.customer, b.date, b.cents, o.customer, o.date, o.cents "
        "FROM __lsw_batch b JOIN orders o ON o.id = b.id"
    ).fetchall()
    for row in existing:
        if row[1] != row[4] or row[2] != row[5] or row[3] != row[6]:
            raise CliError(4, "conflicting values for an existing order id")
        unchanged += 1

    inserted = len(orders) - unchanged
    stored = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    if stored + inserted > MAX_ORDERS_PER_DATABASE:
        raise CliError(2, "database would exceed the order limit")

    if inserted:
        connection.execute(
            "INSERT INTO orders (id, customer, date, cents) "
            "SELECT id, customer, date, cents FROM __lsw_batch b "
            "WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.id = b.id)"
        )
    return {"inserted": inserted, "unchanged": unchanged}


def load_orders(input_path: str, db_path: str) -> dict[str, int]:
    document = _parse_json(_read_input(input_path))
    orders = validate_orders(document)

    _check_db_target(db_path)
    connection = _connect(db_path, read_only=False)
    try:
        connection.execute("BEGIN TRANSACTION")
        try:
            _ensure_table(connection)
            result = _apply_orders(connection, orders)
            connection.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(Exception):
                connection.execute("ROLLBACK")
            raise
    except CliError:
        raise
    except Exception:
        raise CliError(3, "database operation failed") from None
    finally:
        with contextlib.suppress(Exception):
            connection.close()
    return result


def summarize(db_path: str, month: str | None) -> list[dict]:
    if month is not None:
        _validate_month(month)
    _check_db_target(db_path)
    if not os.path.exists(db_path):
        raise CliError(3, "database does not exist")
    connection = _connect(db_path, read_only=True)
    try:
        if _table_state(connection) != "compatible":
            raise CliError(3, "database schema is incompatible with orders")
        query = (
            "SELECT strftime(date, '%Y-%m') AS month, "
            "COUNT(*), COUNT(DISTINCT customer), SUM(cents) "
            "FROM orders"
        )
        params: list[str] = []
        if month is not None:
            query += " WHERE strftime(date, '%Y-%m') = ?"
            params.append(month)
        query += " GROUP BY 1 ORDER BY 1"
        rows = connection.execute(query, params).fetchall()
    except CliError:
        raise
    except Exception:
        raise CliError(3, "database read failed") from None
    finally:
        with contextlib.suppress(Exception):
            connection.close()

    return [
        {
            "month": row[0],
            "orders": int(row[1]),
            "customers": int(row[2]),
            "cents": str(int(row[3])),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _emit(payload) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _split_command(args: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    options: dict[str, str] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("-") and token != "-":
            name, separator, inline = token.partition("=")
            if name not in ("--db", "--month"):
                raise CliError(2, "unknown option")
            if name in options:
                raise CliError(2, f"duplicate option: {name}")
            if separator:
                value = inline
            else:
                index += 1
                if index >= len(args):
                    raise CliError(2, f"missing value for {name}")
                value = args[index]
            if value == "":
                raise CliError(2, f"missing value for {name}")
            options[name] = value
        else:
            positional.append(token)
        index += 1
    return positional, options


def _require(options: dict[str, str], name: str) -> str:
    if name not in options:
        raise CliError(2, f"missing required option: {name}")
    return options[name]


def run(argv: list[str]) -> int:
    if argv == ["--version"]:
        sys.stdout.write(f"{PRODUCT} {VERSION}\n")
        sys.stdout.flush()
        return 0
    if argv in (["--help"], ["-h"]):
        sys.stdout.write(HELP_TEXT)
        sys.stdout.flush()
        return 0
    if not argv:
        raise CliError(2, "missing command; try --help")
    if argv[0] in ("--version", "--help", "-h"):
        raise CliError(2, "this option must be used alone")

    command, rest = argv[0], argv[1:]
    if command == "load":
        positional, options = _split_command(rest)
        if len(positional) != 1:
            raise CliError(2, "load requires exactly one INPUT path")
        if "--month" in options:
            raise CliError(2, "load does not accept --month")
        db_path = _require(options, "--db")
        result = load_orders(positional[0], db_path)
        _emit(result)
        return 0
    if command == "summary":
        positional, options = _split_command(rest)
        if positional:
            raise CliError(2, "summary accepts no positional arguments")
        db_path = _require(options, "--db")
        months = summarize(db_path, options.get("--month"))
        _emit({"months": months})
        return 0
    raise CliError(2, "unknown command")


def main() -> int:
    _bootstrap_venv()
    try:
        return run(sys.argv[1:])
    except CliError as error:
        sys.stderr.write(f"error: {error.message}\n")
        sys.stderr.flush()
        return error.code
    except OSError:
        # Redirect the descriptor before Python retries its buffered flush at exit.
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        sys.stderr.write("error: cannot write output\n")
        sys.stderr.flush()
        return 3
    except KeyboardInterrupt:
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

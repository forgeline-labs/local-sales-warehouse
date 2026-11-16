# Changelog

All notable changes to this project are documented here. Product versions are
independent of the specification version.

## [0.1.0] - 2026-11-16

Initial release implementing local-sales-warehouse spec v0.1.4.

### Added

- `app load INPUT --db DB`: strict JSON validation (UTF-8 without BOM, 4 MiB
  inclusive, 10000 orders, exact keys, ASCII identifiers, strict dates and
  integer `cents`), atomic DuckDB import into the public `orders` table,
  idempotent re-imports, conflict detection with exit 4 and rollback of the
  whole batch.
- `app summary --db DB [--month YYYY-MM]`: monthly order counts, distinct
  customers and exact `HUGEINT` cent sums rendered as decimal strings.
- `--version` (exactly `local-sales-warehouse 0.1.0`) and `--help`.
- Idempotent `./setup` / `python setup` creating `.venv` with the pinned
  `duckdb==1.4.3` runtime dependency.
- Black-box acceptance tests in `tests/`, traceable to AC-1..AC-14.
- CI on Ubuntu and macOS with Python 3.12 and `PYTHONUTF8=1`.

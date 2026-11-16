# Normative ambiguities and public-test coverage

The specification (v0.1.4, §8) resolves fifteen ambiguous points. This file
records where the public black-box suite covers each resolution. Resolutions
that the spec itself declares out of scope for testing remain subject to
inspection rather than claimed as covered.

| # | Resolution (summary) | Public coverage |
| --- | --- | --- |
| 1 | INPUT with BOM → exit 2, no DB created/modified | `ValidationTests.test_bom_is_rejected_without_touching_database` |
| 2 | Size limit inclusive at 4194304 bytes; above → exit 2 before mutation | `ValidationTests.test_input_size_limit_is_inclusive` |
| 3 | External DuckDB lock in another process → exit 3, no success/truncation | `FailureAndAtomicityTests.test_external_lock_is_an_io_error_and_preserves_data` |
| 4 | DuckDB transactions mandatory; persistence observable after close/reopen | `DurabilityTests`, `FailureAndAtomicityTests` |
| 5 | Unicode escapes/key order not criteria; id/customer ASCII | `LoadTests.test_key_order_and_unicode_escapes_are_not_criteria`; `ValidationTests.test_invalid_identifiers` |
| 6 | Invalid month → exit 2; valid month without data → `{"months":[]}` | `ValidationTests.test_invalid_month`, `SummaryTests.test_ac5_month_filter` |
| 7 | Accumulated 10^9 order limit, inclusive, exit 2 before commit | Enforced by the implementation; not exercised (materializing 10^9 rows is out of scope). Per-file limit of 10000 is tested in `LoadTests.test_order_limits_at_and_above_boundaries`. |
| 8 | Incompatible `orders` table (missing/extra columns, types, constraints) → exit 3, no silent migration | `FailureAndAtomicityTests.test_incompatible_orders_table_is_rejected_without_migration`, `DurabilityTests.test_orders_table_is_public_duckdb_schema` |
| 9 | Customer/date/cents difference for same id → conflict exit 4 | `LoadTests.test_id_conflict_on_date_or_cents` |
| 10 | Any conflict/validation invalidates the whole batch; rollback | `LoadTests.test_ac3_conflict_rolls_back_whole_batch` |
| 11 | JSON stdout: one document, one line, one final LF, no BOM/logs | `WarehouseTestCase.assert_success` used by every success test |
| 12 | `summary` with missing DB → exit 3 without creating a file | `FailureAndAtomicityTests.test_ac10_summary_without_database_exits_3_and_creates_nothing` |
| 13 | One JSON document per invocation; concatenated objects invalid | `ValidationTests.test_raw_json_rejections` |
| 14 | Paths with spaces/Unicode mandatory; spaces in id/customer invalid | `LoadTests.test_ac13_paths_with_spaces_and_unicode`, `ValidationTests.test_invalid_identifiers` |
| 15 | JSON temp-file removal out of scope for DB; storage failure preserves committed data | `FailureAndAtomicityTests.test_ac12_storage_failure_keeps_committed_data_and_no_partial_batch` |

## Notes

- The hidden suite is external to this clone and was not read. These public
  tests are the implementer's own black-box tests and are not claimed to be
  independent.
- The read-only-directory write-failure fixture skips with a justification
  when the process runs as root or the filesystem does not enforce directory
  permissions; the external-lock fixture has no such skip.

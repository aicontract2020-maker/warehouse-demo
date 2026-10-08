# Persistence Contract: Pick Zone Workbook

Status: Frozen at Gate 2 on 2026-10-08
Version: 1.0
Schema version: 1

## 1. File and ownership rules

- One workbook is active.
- The application is the only writer while running.
- Workbook path must be under configured data directory.
- Main, temporary, and backup files live in the same directory.
- Credentials, frames, and binary evidence never enter workbook cells.
- Header names and order are fixed for schema version 1.

## 2. Worksheet headers

```text
WorkbookMeta:
key,value,updated_at

Skus:
sku_id,description,unit,detector_class,reference_image_path,active,created_at,updated_at

Locations:
location_id,description,location_type,active,created_at,updated_at

Inventory:
inventory_id,location_id,sku_id,quantity,unit,version,updated_at,last_event_id

Tasks:
task_id,order_id,sku_id,expected_quantity,unit,source_location_id,
destination_location_id,status,selected,created_at,updated_at,completed_event_id

SourceProfiles:
source_profile_id,name,source_type,file_path,camera_index,network_url_redacted,
analysis_fps,preview_fps,requested_width,requested_height,reconnect_enabled,active,
created_at,updated_at

ZoneProfiles:
zone_profile_id,source_profile_id,name,version,source_width,source_height,
shelf_polygon_json,interaction_polygon_json,exit_polygon_json,counting_line_json,
shelf_side_sign,uncertainty_band_norm,crossing_confirm_frames,quiet_seconds,
stable_seconds,hard_idle_seconds,merge_window_seconds,active,created_at,updated_at

Sessions:
session_id,source_profile_id,zone_profile_id,task_id,sku_id,location_id,status,
started_at,ended_at,captured_frames,analyzed_frames,dropped_frames,late_results,
mean_latency_ms,p95_latency_ms,error_code

Events:
event_id,session_id,task_id,zone_profile_id,zone_version,state,observed_sku_id,
observed_quantity,observed_unit,observed_source_location_id,final_sku_id,
final_quantity,final_unit,final_source_location_id,aggregate_confidence,
integrity_flags_json,review_reason,decision,started_at,ended_at,evidence_path,
created_at,decided_at,applied_at,mutation_id

EventActions:
action_id,event_id,action_sequence,track_id,transition_index,action_type,delta,
source_timestamp_ms,frame_sequence,confidence,bbox_json,created_at

Reviews:
review_id,event_id,decision,operator_id,original_values_json,final_values_json,
reason,created_at

AuditLog:
audit_id,timestamp,operator_id,action,entity_type,entity_id,request_id,before_json,
after_json,result,error_code,details_json

MutationReceipts:
mutation_id,idempotency_key,event_id,mutation_hash,committed_at,
workbook_version_before,workbook_version_after,result_json
```

Line wrapping above is documentation only; actual header values contain no newlines.

## 3. Repository interface

```python
load_snapshot(path) -> WorkbookSnapshot
validate_snapshot(snapshot) -> ValidationReport
commit(mutation: WorkbookMutation) -> CommitReceipt
reset_scenario(scenario_id, request_id) -> CommitReceipt
```

Only the writer worker calls `commit` or `reset_scenario`.

## 4. WorkbookMutation

```text
mutation_id: UUID
idempotency_key: string
request_id: UUID|null
expected_workbook_version: int
preconditions: tuple[MutationPrecondition, ...]
upserts: mapping[sheet_name, tuple[row, ...]]
appends: mapping[sheet_name, tuple[row, ...]]
deletes: empty in v1 except controlled scenario reset
canonical_effects_hash: SHA-256
```

## 5. Commit semantics

1. If idempotency key exists with same hash, return original receipt.
2. If key exists with different hash, return `IDEMPOTENCY_CONFLICT`.
3. Validate expected workbook version and all row preconditions.
4. Apply mutation to a cloned in-memory snapshot.
5. Validate full resulting snapshot.
6. Save, fsync, reopen, and verify temporary workbook.
7. Atomically replace main workbook and retain one backup.
8. Publish new snapshot only after replacement succeeds.

No caller may treat a mutation as committed before receiving CommitReceipt.

## 6. Typed failures

| Code | Meaning |
|---|---|
| `WORKBOOK_NOT_FOUND` | configured file absent |
| `WORKBOOK_LOCKED` | exclusive replacement unavailable |
| `WORKBOOK_CORRUPT` | workbook cannot open |
| `SCHEMA_VERSION_UNSUPPORTED` | unknown schema version |
| `SHEET_MISSING` | required sheet absent |
| `HEADER_MISMATCH` | exact columns/order differ |
| `ROW_VALIDATION_ERROR` | a cell violates entity schema |
| `REFERENTIAL_INTEGRITY_ERROR` | foreign reference missing |
| `DUPLICATE_KEY` | logical/primary key duplicate |
| `STALE_SNAPSHOT` | workbook/inventory version changed |
| `IDEMPOTENCY_CONFLICT` | key reused for different effects |
| `INSUFFICIENT_INVENTORY` | mutation would create negative stock |
| `TEMP_SAVE_FAILED` | temporary save failed |
| `ATOMIC_REPLACE_FAILED` | main file unchanged; replace failed |
| `POST_WRITE_VERIFY_FAILED` | temporary/result verification failed |

All failures disable auto-approval until repository status returns `ready`.

## 7. Fixture template

A checked-in schema-1 template includes:

- one bag SKU and one case SKU
- pick and staging locations
- inventory balances
- one open selected task
- one file/replay source profile
- one corresponding zone profile
- empty sessions/events/actions/reviews/receipts
- initial audit row documenting fixture creation

## 8. AC Coverage

AC-5, AC-21 through AC-26, AC-30, AC-E7, AC-E8.

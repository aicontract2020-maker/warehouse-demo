# Data Model: Pick Zone Real-Time Counting Demo

Status: Approved at Gate 2 on 2026-10-08
Version: 1.0
Last updated: 2026-10-08

## Spec Reference

Implements: `specs/pick-zone-demo/spec.md` v1.0

## 1. Storage Model

The demo stores business data in one Excel-compatible `.xlsx` workbook. Each entity maps to
one worksheet with a fixed header row. The application loads all sheets into typed in-memory
maps, validates referential integrity, and serializes all mutations through one writer.

Excel provides no native primary keys, foreign keys, checks, or transactions. Every
constraint below is enforced at workbook load and before each mutation. A workbook that
violates a mandatory constraint is read-only until corrected or reset.

All timestamps are ISO-8601 UTC strings with a `Z` suffix. Quantities are integers in v1.
JSON-valued cells contain canonical compact JSON with sorted object keys.

## 2. Worksheet Summary

| Worksheet | Entity | Primary key | Main purpose |
|---|---|---|---|
| `WorkbookMeta` | WorkbookMetadata | `key` | schema and scenario metadata |
| `Skus` | Sku | `sku_id` | item master |
| `Locations` | Location | `location_id` | warehouse location master |
| `Inventory` | InventoryBalance | `inventory_id` | current quantity by location and SKU |
| `Tasks` | PickTask | `task_id` | mock WMS pick tasks |
| `SourceProfiles` | SourceProfile | `source_profile_id` | saved source settings without credentials |
| `ZoneProfiles` | ZoneProfile | `zone_profile_id` | normalized spatial configuration |
| `Sessions` | MonitoringSession | `session_id` | monitoring lifecycle and metrics |
| `Events` | PickEvent | `event_id` | observed/final business event |
| `EventActions` | EventAction | `action_id` | ordered pick/return evidence |
| `Reviews` | ReviewDecision | `review_id` | human decision history |
| `AuditLog` | AuditRecord | `audit_id` | append-only operational audit |
| `MutationReceipts` | MutationReceipt | `mutation_id` | idempotency and commit receipt |

## 3. Entities

### 3.1 WorkbookMetadata (`WorkbookMeta`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `key` | string | PK, non-empty | metadata key |
| `value` | string | required | metadata value |
| `updated_at` | UTC timestamp | required | last update |

Required rows:

| key | Meaning |
|---|---|
| `schema_version` | exact workbook schema version, initially `1` |
| `workbook_id` | stable UUID for this workbook |
| `scenario_id` | reset scenario identifier |
| `created_at` | workbook creation timestamp |
| `last_committed_at` | last successful atomic mutation |
| `last_mutation_id` | last committed mutation ID |

### 3.2 Sku (`Skus`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `sku_id` | string | PK, non-empty | business SKU |
| `description` | string | required | display name |
| `unit` | enum | `bag`, `case`, `piece` | counting unit |
| `detector_class` | string | required | model class mapped to SKU |
| `reference_image_path` | relative path | optional | local display/reference image |
| `active` | boolean | required | selectable status |
| `created_at` | UTC timestamp | required | creation |
| `updated_at` | UTC timestamp | required | last update |

Constraints:

- One demo zone may map one detector class to one active SKU.
- A reference path must stay under the configured data directory.
- Inactive SKUs remain readable for historical events.

### 3.3 Location (`Locations`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `location_id` | string | PK, non-empty | location code |
| `description` | string | required | display name |
| `location_type` | enum | `pick_zone`, `staging` | location role |
| `active` | boolean | required | selectable status |
| `created_at` | UTC timestamp | required | creation |
| `updated_at` | UTC timestamp | required | last update |

### 3.4 InventoryBalance (`Inventory`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `inventory_id` | string | PK | stable UUID |
| `location_id` | string | FK Locations | owning location |
| `sku_id` | string | FK Skus | stock item |
| `quantity` | integer | >= 0 | current balance |
| `unit` | enum | must equal SKU unit | balance unit |
| `version` | integer | >= 1 | optimistic mutation version |
| `updated_at` | UTC timestamp | required | last mutation time |
| `last_event_id` | string | FK Events, optional | last applying event |

Logical unique constraint: `(location_id, sku_id)`.

Mutation precondition: update must supply expected `version` and current `quantity`. A mismatch
returns `STALE_SNAPSHOT` and does not write.

### 3.5 PickTask (`Tasks`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `task_id` | string | PK | task identifier |
| `order_id` | string | required | parent order |
| `sku_id` | string | FK Skus | expected SKU |
| `expected_quantity` | integer | > 0 | expected pick |
| `unit` | enum | equals SKU unit | expected unit |
| `source_location_id` | string | FK Locations | pick location |
| `destination_location_id` | string | FK Locations | staging destination |
| `status` | enum | `open`, `in_progress`, `completed`, `cancelled` | lifecycle |
| `selected` | boolean | max one true | current demo task |
| `created_at` | UTC timestamp | required | creation |
| `updated_at` | UTC timestamp | required | last transition |
| `completed_event_id` | string | FK Events, optional | applying event |

Constraints:

- At most one task is selected.
- Only `open` or `in_progress` tasks may be selected for monitoring.
- A completed task has exactly one `completed_event_id`.
- Reset may restore fixture task states but writes an audit record.

### 3.6 SourceProfile (`SourceProfiles`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `source_profile_id` | string | PK | profile ID |
| `name` | string | required | display name |
| `source_type` | enum | `file`, `camera`, `network`, `replay` | adapter |
| `file_path` | relative path | optional | uploaded/local media |
| `camera_index` | integer | optional, >= 0 | local device index |
| `network_url_redacted` | string | optional | URL without credentials |
| `analysis_fps` | integer | 3, 4, or 5 | analysis target |
| `preview_fps` | integer | 1-10 | preview cap |
| `requested_width` | integer | > 0 | requested capture width |
| `requested_height` | integer | > 0 | requested capture height |
| `reconnect_enabled` | boolean | required | network retry policy |
| `active` | boolean | max one true | selected source profile |
| `created_at` | UTC timestamp | required | creation |
| `updated_at` | UTC timestamp | required | update |

Exactly one source-specific field is populated according to `source_type`. Network
credentials are never persisted. A runtime secret reference may be held in memory only.

### 3.7 ZoneProfile (`ZoneProfiles`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `zone_profile_id` | string | PK | zone ID |
| `source_profile_id` | string | FK SourceProfiles | associated source |
| `name` | string | required | display name |
| `version` | integer | >= 1 | configuration version |
| `source_width` | integer | > 0 | calibration frame width |
| `source_height` | integer | > 0 | calibration frame height |
| `shelf_polygon_json` | JSON points | valid normalized polygon | shelf side |
| `interaction_polygon_json` | JSON points | valid normalized polygon | interaction region |
| `exit_polygon_json` | JSON points | valid normalized polygon | exit region |
| `counting_line_json` | JSON two points | valid normalized line | direction line |
| `shelf_side_sign` | integer | -1 or 1 | signed line side that means shelf |
| `uncertainty_band_norm` | decimal | 0-0.2 | no-decision band |
| `crossing_confirm_frames` | integer | >= 2 | debounce observations |
| `quiet_seconds` | decimal | > 0, default 3 | settling threshold |
| `stable_seconds` | decimal | > 0, default 2 | finish threshold |
| `hard_idle_seconds` | decimal | >= quiet + stable, default 10 | forced review threshold |
| `merge_window_seconds` | decimal | >= 0, default 10 | business merge threshold |
| `active` | boolean | max one per source | selected zone |
| `created_at` | UTC timestamp | required | creation |
| `updated_at` | UTC timestamp | required | update |

Point format: `[[x0,y0],[x1,y1],...]` with each coordinate in `[0,1]`. Polygons require
three or more non-collinear points and no self-intersection.

### 3.8 MonitoringSession (`Sessions`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `session_id` | UUID | PK | monitoring run |
| `source_profile_id` | string | FK SourceProfiles | source config |
| `zone_profile_id` | string | FK ZoneProfiles | zone config |
| `task_id` | string | FK Tasks | selected task |
| `sku_id` | string | FK Skus | zone SKU snapshot |
| `location_id` | string | FK Locations | zone location snapshot |
| `status` | enum | `starting`, `running`, `paused`, `stopping`, `stopped`, `failed` | lifecycle |
| `started_at` | UTC timestamp | required | start |
| `ended_at` | UTC timestamp | optional | finish |
| `captured_frames` | integer | >= 0 | capture metric |
| `analyzed_frames` | integer | >= 0 | analysis metric |
| `dropped_frames` | integer | >= 0 | stale/overflow count |
| `late_results` | integer | >= 0 | discarded out-of-order results |
| `mean_latency_ms` | decimal | >= 0 | result metric |
| `p95_latency_ms` | decimal | >= 0 | result metric |
| `error_code` | string | optional | terminal typed error |

Only one session may be in `starting`, `running`, `paused`, or `stopping` at once.

### 3.9 PickEvent (`Events`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `event_id` | UUID | PK | immutable event ID |
| `session_id` | UUID | FK Sessions | owning session |
| `task_id` | string | FK Tasks | task snapshot |
| `zone_profile_id` | string | FK ZoneProfiles | zone snapshot |
| `zone_version` | integer | required | applied version |
| `state` | enum | event lifecycle | current state |
| `observed_sku_id` | string | FK Skus | system observation |
| `observed_quantity` | integer | >= 0 | net observed quantity |
| `observed_unit` | enum | required | observed unit |
| `observed_source_location_id` | string | FK Locations | observed source |
| `final_sku_id` | string | FK Skus, optional | approved/corrected SKU |
| `final_quantity` | integer | >= 0, optional | approved/corrected quantity |
| `final_unit` | enum | optional | approved/corrected unit |
| `final_source_location_id` | string | FK Locations, optional | approved/corrected source |
| `aggregate_confidence` | decimal | 0-1 | minimum mandatory evidence confidence |
| `integrity_flags_json` | JSON string array | canonical | ambiguity/interruption flags |
| `review_reason` | string enum | optional | primary review reason |
| `decision` | enum | `pending`, `auto_approved`, `manual_approved`, `rejected`, `no_op` | final decision |
| `started_at` | UTC timestamp | required | first confirmed action |
| `ended_at` | UTC timestamp | required after completion | completion |
| `evidence_path` | relative path | optional | local evidence clip |
| `created_at` | UTC timestamp | required | record creation |
| `decided_at` | UTC timestamp | optional | final decision |
| `applied_at` | UTC timestamp | optional | workbook mutation time |
| `mutation_id` | UUID | FK MutationReceipts, optional | idempotent commit |

Event states: `active`, `settling`, `completed`, `review_required`, `approved`,
`rejected`. Observed fields and actions are immutable after completion. Final fields are
empty until approval and preserve human corrections separately.

### 3.10 EventAction (`EventActions`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `action_id` | UUID | PK | action ID |
| `event_id` | UUID | FK Events | parent event |
| `action_sequence` | integer | unique per event, starts 1 | order |
| `track_id` | integer | >= 1 | source-session track |
| `transition_index` | integer | >= 1 | transitions for track |
| `action_type` | enum | `pick`, `return` | direction |
| `delta` | integer | +1 or -1 | quantity effect |
| `source_timestamp_ms` | integer | >= 0 | media/source time |
| `frame_sequence` | integer | >= 0 | analyzed frame |
| `confidence` | decimal | 0-1 | crossing confidence |
| `bbox_json` | JSON array | four numbers | evidence bbox |
| `created_at` | UTC timestamp | required | record time |

Logical unique constraint: `(event_id, track_id, transition_index)`.

### 3.11 ReviewDecision (`Reviews`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `review_id` | UUID | PK | decision ID |
| `event_id` | UUID | FK Events | reviewed event |
| `decision` | enum | `approve_observed`, `approve_edited`, `reject` | action |
| `operator_id` | string | required, default `demo_operator` | actor |
| `original_values_json` | JSON object | required | immutable observed result |
| `final_values_json` | JSON object | required except reject | approved result |
| `reason` | string | required for edit/reject | explanation |
| `created_at` | UTC timestamp | required | decision time |

Every review command appends a row. The latest successful decision determines the event, but
prior attempts remain visible.

### 3.12 AuditRecord (`AuditLog`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `audit_id` | UUID | PK | record ID |
| `timestamp` | UTC timestamp | required | occurrence |
| `operator_id` | string | required | actor/system |
| `action` | string enum | required | audited operation |
| `entity_type` | string | required | affected entity |
| `entity_id` | string | required | affected ID |
| `request_id` | UUID | optional | API request correlation |
| `before_json` | JSON | optional | prior state |
| `after_json` | JSON | optional | resulting state |
| `result` | enum | `success`, `failure` | outcome |
| `error_code` | string | optional | typed failure |
| `details_json` | JSON | optional | redacted context |

Audit rows are append-only and credentials/frames are prohibited.

### 3.13 MutationReceipt (`MutationReceipts`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `mutation_id` | UUID | PK | commit identifier |
| `idempotency_key` | string | unique | request/event command key |
| `event_id` | UUID | FK Events, optional | applying event |
| `mutation_hash` | SHA-256 hex | required | canonical effects hash |
| `committed_at` | UTC timestamp | required | atomic replacement time |
| `workbook_version_before` | integer | >= 0 | precondition |
| `workbook_version_after` | integer | = before + 1 | result |
| `result_json` | JSON | required | stable commit receipt |

## 4. Relationships

- Sku has many InventoryBalance, PickTask, MonitoringSession, and PickEvent records.
- Location has many InventoryBalance records and is referenced by PickTask and PickEvent.
- SourceProfile has many ZoneProfile and MonitoringSession records.
- ZoneProfile has many MonitoringSession and PickEvent records.
- PickTask has many observed PickEvent records but at most one completing approved event.
- MonitoringSession has many PickEvent records.
- PickEvent has many EventAction and ReviewDecision records and at most one applying MutationReceipt.
- MutationReceipt applies at most one PickEvent in v1.
- AuditRecord may refer to any entity without enforcing a cross-sheet foreign key.

## 5. Logical Indexes

The workbook is loaded into these in-memory indexes:

| Index | Key | Purpose |
|---|---|---|
| `sku_by_id` | `sku_id` | SKU lookup |
| `location_by_id` | `location_id` | location lookup |
| `inventory_by_location_sku` | `(location_id, sku_id)` | balance and uniqueness |
| `task_by_id` | `task_id` | task lookup |
| `selected_task` | singleton | readiness |
| `source_profile_by_id` | `source_profile_id` | source lookup |
| `active_source_profile` | singleton | readiness |
| `zone_by_source_version` | `(source_profile_id, version)` | event audit |
| `active_zone_by_source` | `source_profile_id` | readiness |
| `session_by_id` | `session_id` | recovery |
| `event_by_id` | `event_id` | idempotency/review |
| `actions_by_event` | `event_id` ordered by sequence | timeline |
| `reviews_by_event` | `event_id` ordered by created time | audit |
| `receipt_by_idempotency_key` | `idempotency_key` | duplicate prevention |

## 6. Cross-Entity Constraints

1. Selected task SKU/unit/source location equal the active zone assignment before monitoring.
2. A monitoring session snapshots source, zone version, task, SKU, and location and never
   changes them in place.
3. A task is completed only in the same mutation that applies its approved event.
4. An approved pick decrements source inventory and increments destination inventory if a
   destination balance exists or is created by the same mutation.
5. Inventory never goes below zero.
6. An event with integrity flags cannot be `auto_approved`.
7. `applied_at` requires a valid MutationReceipt and approved final values.
8. A MutationReceipt idempotency key cannot map to two mutation hashes.
9. Relative paths cannot escape configured application data directories.
10. Workbook schema errors disable writes and automatic approval.

## 7. Mutation Transactions

### 7.1 Automatic approval mutation

Preconditions:

- event not previously applied
- task open/in-progress and fields match
- source inventory version and quantity match expected snapshot
- event confidence/integrity satisfy policy
- evidence exists
- workbook writable

Effects in one workbook replacement:

1. append/update completed Event
2. append EventActions
3. set final values and `auto_approved`
4. decrement source Inventory and increment its version
5. increment destination Inventory and increment/create its version
6. complete Task and attach event
7. append AuditRecord
8. append MutationReceipt
9. update WorkbookMeta version and last commit

### 7.2 Manual decision mutation

Uses the same preconditions/effects, plus appends ReviewDecision and uses final edited or
observed values. Reject writes Event, ReviewDecision, AuditRecord, and receipt but no
inventory/task quantity mutation.

### 7.3 Scenario reset mutation

Reset is limited to named fixture scenarios. It stops workers first, replaces scenario-owned
business sheets from a checked-in template, retains or archives prior audit based on scenario
policy, and records a new reset AuditRecord.

## 8. Workbook File Protocol

1. Acquire the application writer lock.
2. Reload and validate the current workbook if its file signature changed externally.
3. Validate mutation preconditions against the latest committed snapshot.
4. Apply changes to a cloned in-memory workbook.
5. Save to `.<name>.<mutation_id>.tmp.xlsx` in the same directory.
6. Flush and fsync the temporary file.
7. Verify the temporary workbook can reopen and required rows/receipt are present.
8. Move the current workbook to a single `.bak` path.
9. Atomically replace the main workbook with the temporary file.
10. Update in-memory snapshot and return CommitReceipt.
11. On failure, retain the prior main workbook, clean the temporary file when possible, and
    publish a typed persistence failure.

## 9. Schema Evolution

### Schema 1: Initial Pick Zone demo

Creates every worksheet and field defined in this document.

Rollback: not applicable before first release; reset from the checked-in schema-1 template.

Future schema changes require:

- increment `WorkbookMeta.schema_version`
- append a migration function with input/output versions
- preserve observed events and audit rows
- produce a backup before migration
- document rollback or explicitly mark irreversible with human approval

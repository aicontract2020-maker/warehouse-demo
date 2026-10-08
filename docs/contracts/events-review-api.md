# API Contract: Events, Review, and Evidence

Status: Frozen at Gate 2 on 2026-10-08
Version: 1.0

## Common Rules

Base path `/api/v1`; loopback-only. Every mutation accepts header `Idempotency-Key` containing
a UUID. Repeating a key with identical canonical request returns the original receipt.
Repeating it with a different request returns `IDEMPOTENCY_CONFLICT` 409.

## GET /events

Query parameters:

| Name | Type | Default | Description |
|---|---|---|---|
| `decision` | string | all | filter decision |
| `session_id` | UUID | all | filter session |
| `limit` | integer 1-200 | 50 | page size |
| `cursor` | string | none | opaque next page |

Returns summaries ordered newest first.

## GET /events/{event_id}

**Success 200**

```json
{
  "request_id": "uuid",
  "event": {
    "event_id": "uuid",
    "session_id": "uuid",
    "task_id": "PICK-1",
    "state": "review_required",
    "observed": {
      "sku_id": "RICE-25KG-A",
      "quantity": 7,
      "unit": "bag",
      "source_location_id": "A-03-01"
    },
    "final": null,
    "aggregate_confidence": 0.997,
    "integrity_flags": [],
    "review_reason": "QUANTITY_MISMATCH",
    "decision": "pending",
    "started_at": "timestamp",
    "ended_at": "timestamp",
    "evidence_url": "/api/v1/events/uuid/evidence"
  },
  "actions": [
    {
      "action_id": "uuid",
      "sequence": 1,
      "track_id": 4,
      "type": "pick",
      "delta": 1,
      "source_timestamp_ms": 1200,
      "confidence": 0.998,
      "bbox": [10,20,100,120]
    }
  ],
  "reviews": []
}
```

**Errors:** `EVENT_NOT_FOUND` 404.

## POST /events/{event_id}/approve-observed

Approves observed values without modification.

**Request:** `{"reason":"optional note"}`

**Success 200**

```json
{
  "request_id": "uuid",
  "event_id": "uuid",
  "decision": "manual_approved",
  "mutation_id": "uuid",
  "inventory": [{"location_id":"A-03-01","sku_id":"RICE-25KG-A","quantity":35,"version":2}],
  "task": {"task_id":"PICK-1","status":"completed"}
}
```

## POST /events/{event_id}/approve-edited

**Request**

```json
{
  "final_sku_id": "RICE-25KG-A",
  "final_quantity": 8,
  "final_unit": "bag",
  "final_source_location_id": "A-03-01",
  "reason": "Manual count confirmed eight bags"
}
```

Reason is mandatory and non-whitespace.

## POST /events/{event_id}/reject

**Request**

```json
{"reason": "Track merged with worker"}
```

Reject persists decision/audit but does not change inventory or complete the task.

## Automatic approval internal command

The analyzer may submit a completed event to the same business service with command
`AUTO_APPROVE_IF_ELIGIBLE`. It uses event ID as idempotency key and must satisfy every AC-20
precondition. No HTTP endpoint bypasses this policy.

## GET /events/{event_id}/evidence

Returns MP4 with byte-range support.

**Errors:** `EVENT_NOT_FOUND` 404, `EVIDENCE_UNAVAILABLE` 404,
`PATH_NOT_ALLOWED` 403.

## Mutation errors

| Status | Code | Condition |
|---|---|---|
| 409 | `EVENT_NOT_REVIEWABLE` | final decision already exists or state invalid |
| 409 | `IDEMPOTENCY_CONFLICT` | key reused with different payload |
| 409 | `TASK_STATE_CHANGED` | task no longer open/in-progress |
| 409 | `STALE_SNAPSHOT` | inventory version changed |
| 409 | `INSUFFICIENT_INVENTORY` | source would go negative |
| 422 | `REVIEW_REASON_REQUIRED` | edit/reject reason absent |
| 422 | `FINAL_VALUES_INVALID` | SKU/unit/location/quantity invalid |
| 503 | `PERSISTENCE_BLOCKED` | workbook cannot commit |

## AC Coverage

AC-18 through AC-23, AC-25 through AC-27, AC-29, AC-E7.

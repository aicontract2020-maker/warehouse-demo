# Contract: Live Runtime Stream

Status: Frozen at Gate 2 on 2026-10-08
Version: 1.0

## WS /api/v1/live

Authentication: none; loopback-only.

After connection the server immediately sends one `snapshot`. Later messages are complete
latest-state snapshots, not mandatory deltas. A slow client retains at most one unsent
snapshot; intermediate snapshots are dropped.

## Server message envelope

```json
{
  "type": "snapshot|error|server_stopping",
  "schema_version": 1,
  "sequence": 120,
  "sent_at": "timestamp",
  "payload": {}
}
```

## Snapshot payload

```json
{
  "monitoring_state": "running",
  "source": {
    "state": "connected",
    "profile_id": "source-1",
    "source_timestamp_ms": 4300,
    "continuity_segment": 0
  },
  "analysis": {
    "last_applied_frame_sequence": 18,
    "detections": [
      {"detection_id":"18:0","class_id":0,"class_name":"bag","confidence":0.98,"bbox":[10,20,80,120]}
    ],
    "tracks": [
      {
        "track_id":4,
        "class_name":"bag",
        "confidence":0.97,
        "bbox":[12,21,82,121],
        "state":"confirmed",
        "line_side":"shelf",
        "ambiguous":false
      }
    ]
  },
  "event": {
    "event_id": "uuid|null",
    "state": "active|settling|review_required|approved|null",
    "pick_count": 3,
    "return_count": 1,
    "net_quantity": 2,
    "last_action_at": "timestamp|null",
    "review_reason": "string|null"
  },
  "task": {
    "task_id": "PICK-1",
    "sku_id": "RICE-25KG-A",
    "expected_quantity": 8,
    "unit": "bag",
    "source_location_id": "A-03-01",
    "status": "open"
  },
  "inventory": {"location_id":"A-03-01","sku_id":"RICE-25KG-A","quantity":42,"version":1},
  "metrics": {
    "capture_fps": 29.7,
    "analysis_fps": 4.0,
    "preview_fps": 10.0,
    "dropped_frames": 83,
    "late_results": 0,
    "result_latency_ms": 190.0,
    "analysis_queue_depth": 0,
    "persistence_queue_depth": 0
  },
  "alerts": []
}
```

Bounding boxes use source pixel coordinates. Zone geometry is fetched from the profile API and
is not repeated in every snapshot.

## Client messages

The client sends no business commands through the WebSocket. It may send
`{"type":"ping","sent_at":"timestamp"}` and receives `pong`. All commands use HTTP contracts.

## Reconnect

On reconnect the client discards prior local snapshots and accepts the first full snapshot.
The backend never replays inventory commands because a client reconnects.

## Closure codes

| Code | Meaning |
|---:|---|
| 1000 | normal client close |
| 1001 | server stopping |
| 1008 | invalid client message |
| 1011 | runtime state unavailable |

## AC Coverage

AC-8 through AC-10, AC-14 through AC-17, AC-27 through AC-29, AC-E5, AC-E6, AC-S1.

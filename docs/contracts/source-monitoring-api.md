# API Contract: Source and Monitoring

Status: Frozen at Gate 2 on 2026-10-08
Version: 1.0

## Common Rules

- Base path: `/api/v1`.
- Authentication: none; server is loopback-only.
- All JSON responses include `request_id`.
- Timestamps are ISO-8601 UTC.
- Errors use:

```json
{
  "request_id": "uuid",
  "error": {
    "code": "MACHINE_CODE",
    "message": "human-readable text",
    "details": {}
  }
}
```

## GET /status

Returns a complete non-video runtime snapshot.

**Success 200**

```json
{
  "request_id": "uuid",
  "application": {"status": "ready|degraded|stopping", "version": "string"},
  "monitoring": {
    "session_id": "uuid|null",
    "state": "stopped|starting|running|paused|stopping|failed",
    "source_profile_id": "string|null",
    "zone_profile_id": "string|null",
    "task_id": "string|null",
    "active_event_id": "uuid|null"
  },
  "source": {
    "state": "closed|opening|connected|reconnecting|ended|failed",
    "continuity_segment": 0,
    "last_frame_at": "timestamp|null",
    "reconnect_attempts": 0,
    "error_code": "string|null"
  },
  "metrics": {
    "captured_frames": 0,
    "analyzed_frames": 0,
    "dropped_frames": 0,
    "late_results": 0,
    "capture_fps": 0.0,
    "analysis_fps": 0.0,
    "preview_fps": 0.0,
    "result_latency_ms": 0.0,
    "result_latency_p95_ms": 0.0,
    "analysis_queue_depth": 0,
    "persistence_queue_depth": 0
  },
  "persistence": {"state": "ready|blocked|failed", "last_commit_at": "timestamp|null"}
}
```

## POST /uploads

Uploads one local video as multipart field `file`.

Constraints:

- supported extensions: `.mp4`, `.mov`, `.avi`, `.mkv`
- file name is sanitized; server chooses stored name
- maximum size is configuration-defined and returned by `GET /capabilities`
- upload directory is the configured local data directory

**Success 201**

```json
{
  "request_id": "uuid",
  "upload_id": "uuid",
  "stored_name": "uuid.mp4",
  "original_name": "pick.mp4",
  "size_bytes": 12345,
  "probe": {"width": 1280, "height": 720, "fps": 30.0, "duration_ms": 10000}
}
```

**Errors:** `UNSUPPORTED_MEDIA` 415, `FILE_TOO_LARGE` 413, `DECODE_FAILED` 422,
`STORAGE_UNAVAILABLE` 503.

## GET /cameras

Probes local camera indexes only on explicit request.

**Success 200**

```json
{
  "request_id": "uuid",
  "cameras": [{"index": 0, "label": "Camera 0", "available": true}]
}
```

**Errors:** `CAMERA_PROBE_FAILED` 503.

## GET /capabilities

Returns allowed source types, formats, analysis rates, upload maximum, and runtime versions.

## POST /monitoring/start

**Request**

```json
{
  "source_profile_id": "source-1",
  "zone_profile_id": "zone-1",
  "task_id": "PICK-1",
  "analysis_fps": 4,
  "network_url_override": "optional runtime-only URL with credentials"
}
```

`network_url_override` is accepted only for a network profile, is held in memory, and is
redacted from every response/log.

**Success 202**

```json
{
  "request_id": "uuid",
  "session_id": "uuid",
  "state": "starting"
}
```

**Errors**

| Status | Code | Condition |
|---|---|---|
| 400 | `VALIDATION_ERROR` | invalid FPS or request |
| 404 | `SOURCE_PROFILE_NOT_FOUND` | missing source |
| 404 | `ZONE_PROFILE_NOT_FOUND` | missing zone |
| 404 | `TASK_NOT_FOUND` | missing task |
| 409 | `MONITORING_ALREADY_ACTIVE` | active session exists |
| 409 | `PROFILE_TASK_MISMATCH` | zone SKU/location differs from task |
| 409 | `PERSISTENCE_BLOCKED` | workbook cannot accept decisions |
| 422 | `SOURCE_OPEN_FAILED` | source cannot open |

## POST /monitoring/pause

Pauses file playback and analysis. Live sources stop analysis and keep only the latest frame;
no event timer advances while paused.

**Success 200:** current monitoring state.  
**Errors:** `INVALID_MONITORING_STATE` 409.

## POST /monitoring/resume

Resumes a paused session.

**Success 200:** current monitoring state.  
**Errors:** `INVALID_MONITORING_STATE` 409, `SOURCE_UNAVAILABLE` 422.

## POST /monitoring/stop

**Request**

```json
{"discard_in_progress": false}
```

If an event is active and `discard_in_progress=false`, return
`ACTIVE_EVENT_REQUIRES_DECISION`. When true, retain an audit entry and discard the incomplete
observation without inventory change.

**Success 200:** stopped session summary.  
**Errors:** `INVALID_MONITORING_STATE` 409, `ACTIVE_EVENT_REQUIRES_DECISION` 409,
`SHUTDOWN_TIMEOUT` 503.

## POST /monitoring/reset

**Request**

```json
{"scenario_id": "pick-zone-default", "confirm": true}
```

Reset is allowed only while stopped. It restores the named fixture and audits the reset.

**Success 200:** workbook/scenario metadata and inventory summary.  
**Errors:** `MONITORING_ACTIVE` 409, `SCENARIO_NOT_FOUND` 404,
`PERSISTENCE_BLOCKED` 503.

## GET /preview.mjpeg

Returns `multipart/x-mixed-replace` annotated JPEG frames. The stream carries no business
commands. Disconnecting a client does not stop monitoring.

**Errors before streaming:** `PREVIEW_UNAVAILABLE` 503.

## AC Coverage

AC-1 through AC-4, AC-6 through AC-8, AC-28 through AC-30, AC-E1, AC-E2, AC-E6, AC-E8,
AC-S2, AC-S3.

# API Contract: Profiles, Tasks, and Inventory

Status: Frozen at Gate 2 on 2026-10-08
Version: 1.0

## Common Rules

Base path `/api/v1`; loopback-only; common error envelope is defined in
`source-monitoring-api.md`.

## GET /source-profiles

Returns profiles with redacted network URLs and no credentials.

## POST /source-profiles

**Request**

```json
{
  "source_profile_id": "source-file-1",
  "name": "Recorded pick",
  "source_type": "file|camera|network|replay",
  "upload_id": "uuid|null",
  "camera_index": 0,
  "network_url": "rtsp://host/path",
  "analysis_fps": 4,
  "preview_fps": 10,
  "requested_width": 1280,
  "requested_height": 720,
  "reconnect_enabled": true,
  "active": true
}
```

Exactly one of `upload_id`, `camera_index`, or `network_url` is accepted for corresponding
source types. Credentials in `network_url` are rejected for persistence; they belong in the
runtime start override.

**Success 201:** persisted redacted SourceProfile.  
**Errors:** `VALIDATION_ERROR` 400, `UPLOAD_NOT_FOUND` 404,
`CREDENTIALS_NOT_PERSISTED` 422, `PERSISTENCE_BLOCKED` 503.

## PUT /source-profiles/{source_profile_id}

Replaces the mutable source profile fields. Active monitoring returns
`PROFILE_IN_USE` 409. Response is the saved profile.

## DELETE /source-profiles/{source_profile_id}

Deletes an unused profile. Historical references or active zones return
`PROFILE_REFERENCED` 409.

## GET /zone-profiles

Query `source_profile_id` is optional. Returns normalized geometry and version.

## POST /zone-profiles

**Request**

```json
{
  "zone_profile_id": "zone-a-03-01",
  "source_profile_id": "source-file-1",
  "name": "A-03-01",
  "source_width": 1280,
  "source_height": 720,
  "shelf_polygon": [[0.05,0.10],[0.50,0.10],[0.50,0.85],[0.05,0.85]],
  "interaction_polygon": [[0.20,0.05],[0.80,0.05],[0.80,0.95],[0.20,0.95]],
  "exit_polygon": [[0.50,0.05],[0.98,0.05],[0.98,0.95],[0.50,0.95]],
  "counting_line": [[0.50,0.05],[0.50,0.95]],
  "shelf_side_sign": -1,
  "uncertainty_band_norm": 0.02,
  "crossing_confirm_frames": 2,
  "quiet_seconds": 3.0,
  "stable_seconds": 2.0,
  "hard_idle_seconds": 10.0,
  "merge_window_seconds": 10.0,
  "active": true
}
```

Server calculates next version for an existing logical zone; historical versions remain
readable.

**Success 201:** saved zone and version.  
**Errors:** `INVALID_POLYGON` 422, `INVALID_LINE` 422,
`PROFILE_DIMENSION_MISMATCH` 422, `SOURCE_PROFILE_NOT_FOUND` 404,
`PERSISTENCE_BLOCKED` 503.

## GET /tasks

Query parameters: `status` and `selected`. Returns task rows plus display SKU/location data.

## POST /tasks/{task_id}/select

Selects one open/in-progress task and clears prior selection atomically.

**Errors:** `TASK_NOT_FOUND` 404, `TASK_NOT_SELECTABLE` 409,
`TASK_PROFILE_MISMATCH` 409.

## GET /inventory

Optional query parameters: `location_id`, `sku_id`. Returns balance and version.

## GET /skus

Returns active SKU master data and configured detector class.

## GET /locations

Returns active locations.

## AC Coverage

AC-5, AC-6, AC-18, AC-19, AC-23 through AC-26, AC-28, AC-30, AC-C2.

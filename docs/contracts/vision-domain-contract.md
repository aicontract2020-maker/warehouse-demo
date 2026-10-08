# Internal Contract: Vision and Event Domain

Status: Frozen at Gate 2 on 2026-10-08
Version: 1.0

## 1. FrameEnvelope

```text
session_id: UUID
continuity_segment: int >= 0
sequence: int >= 0, strictly increasing per session
source_timestamp_ms: int >= 0
captured_monotonic_ns: int >= 0
image_bgr: uint8 ndarray [height,width,3]
width: int > 0
height: int > 0
```

Frames are process-local and are never serialized into workbook or logs.

## 2. Detector interface

```python
detect(frame: FrameEnvelope, context: DetectionContext) -> DetectionBatch
```

`DetectionBatch`:

```text
session_id
continuity_segment
frame_sequence
source_timestamp_ms
started_monotonic_ns
completed_monotonic_ns
model_id
detections: tuple[Detection, ...]
```

`Detection`:

```text
detection_id: "{frame_sequence}:{ordinal}"
class_id: int
class_name: str
confidence: float in [0,1]
bbox_xyxy: float[4] within source dimensions
```

Detector failures return typed `DetectionFailure` to orchestration. They do not return an
empty detection list, because empty is a valid result.

## 3. Tracker interface

```python
update(batch: DetectionBatch) -> TrackingBatch
mark_discontinuity(continuity_segment: int) -> None
reset(session_id: UUID) -> None
```

`TrackObservation`:

```text
track_id: positive int, monotonic within session
class_name: str
confidence: float in [0,1]
bbox_xyxy: float[4]
anchor_xy: float[2]
velocity_xy_per_second: float[2]
state: tentative|confirmed|lost|expired
age_observations: int
missed_duration_ms: int
ambiguous: bool
```

A discontinuity marks all live tracks ambiguous/expired; IDs are never reused.

## 4. DirectionalAction

```text
action_id: UUID
event_id: UUID
action_sequence: positive int
track_id: positive int
transition_index: positive int
action_type: pick|return
delta: +1|-1
source_timestamp_ms: int
frame_sequence: int
confidence: float in [0,1]
bbox_xyxy: float[4]
```

Uniqueness key: `(event_id, track_id, transition_index)`.

## 5. Event state input

For each ordered analyzed frame, the event machine receives:

```text
frame_sequence
source_timestamp_ms
motion_stable: bool
source_health: connected|reconnecting|ended|failed
unresolved_boundary_track_ids: set[int]
actions: tuple[DirectionalAction, ...]
integrity_flags: set[str]
```

## 6. EventSnapshot

```text
event_id: UUID|null
state: idle|active|settling|completed|review_required|approved|rejected
started_source_timestamp_ms: int|null
ended_source_timestamp_ms: int|null
pick_count: int >= 0
return_count: int >= 0
net_quantity: int
actions: immutable ordered tuple
aggregate_confidence: float|null
integrity_flags: immutable set[str]
review_reason: string|null
```

Negative net quantity is invalid and forces review.

## 7. State transitions

| Current | Trigger | Next | Side effect |
|---|---|---|---|
| idle | first confirmed action | active | allocate event ID |
| active | no action for 3s | settling | none |
| settling | new action within merge window | active | append action |
| settling | stable 2s, no unresolved track | completed | reconcile |
| active/settling | source interrupted | review_required | freeze auto-approval |
| active/settling | hard idle 10s | review_required | reason hard timeout |
| completed | eligible match | approved | request atomic mutation |
| completed | mismatch/integrity failure | review_required | no mutation |
| review_required | manual approve | approved | request atomic mutation |
| review_required | manual reject | rejected | audit only |

## 8. Integrity flags and review reasons

Mandatory flags include:

- `SOURCE_INTERRUPTED`
- `TRACK_LOST_NEAR_BOUNDARY`
- `OVERLAPPING_INSEPARABLE`
- `LATE_RESULT_DISCARDED`
- `EVIDENCE_UNAVAILABLE`
- `PERSISTENCE_BLOCKED`

Any mandatory flag prevents auto-approval.

## 9. Ordering contract

The ordered analyzer applies a batch only when its frame sequence is greater than the last
applied sequence in the same continuity segment. A lower/equal sequence is discarded and
increments late-result metrics. A higher continuity segment first expires all prior tracks.

## AC Coverage

AC-9 through AC-20, AC-E2 through AC-E5, AC-E7, AC-S1, AC-C1.

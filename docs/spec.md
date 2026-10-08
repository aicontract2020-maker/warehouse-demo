# Pick Zone Real-Time Counting Demo

Status: Approved at Gate 1 on 2026-10-08
Version: 1.0
Mode: Full
Last updated: 2026-10-08
Source requirements: `docs/laptop-demo-spec.md` sections 3-6, 8-12, 15-16

## 1. Overview

The Pick Zone demo is a local web application that observes one configured warehouse shelf
or floor-pallet position. It accepts a video file, local camera, or network video stream;
detects and tracks configured goods at a sampled rate of 3-5 frames per second; counts goods
moving out of and back into the pick zone; groups movements into a pick event; compares the
net quantity with a mock WMS task; and records approved or review-required events in a local
workbook.

The demo proves a transaction workflow, not universal warehouse perception. It is designed
for a controlled, single-camera, single-zone demonstration on a laptop without requiring a
discrete GPU.

## 2. Actors

### Primary actor

**Demo operator:** configures the input source and pick zone, starts monitoring, observes
tracks and counts, and resolves events that require review.

### Secondary actor

**Warehouse stakeholder:** watches the demo and verifies that a physical pick becomes an
auditable inventory transaction without a PDA scan.

## 3. User Stories

### US-1: Configure a video source

As a demo operator, I want to select a video file, local camera, or network stream so that I
can demonstrate the same workflow with recorded and live inputs.

### US-2: Configure the pick zone

As a demo operator, I want to draw and save the shelf, interaction, exit, and counting
boundaries so that movement direction has an explicit spatial meaning.

### US-3: Observe live goods tracking

As a warehouse stakeholder, I want to see goods bounding boxes, stable track identifiers,
movement direction, and a live net count so that I can understand the evidence behind the
transaction.

### US-4: Complete a pick event

As a demo operator, I want continuous pick and return movements grouped into one event so
that short pauses do not create duplicate WMS transactions.

### US-5: Reconcile with a task

As a demo operator, I want the observed SKU, source location, and net quantity compared with
the selected mock WMS task so that matches can be approved and discrepancies can be reviewed.

### US-6: Audit and recover

As a demo operator, I want persisted events, inventory balances, processing metrics, and
evidence references so that results remain explainable after a restart or failure.

## 4. Product Assumptions for Gate 1

The following assumptions are proposed as v1 decisions. Approving Gate 1 approves them.

1. One monitoring session has exactly one active video source and one active pick zone.
2. A pick zone is assigned one SKU and one inventory location before monitoring begins.
3. Goods are detected as configured supported classes; v1 does not discover arbitrary SKUs.
4. The selected WMS task supplies the expected SKU, unit, quantity, source, and destination.
5. The baseline acceptance path contains one operator and no more than three simultaneously
   visible, independently detectable goods.
6. Goods that are fully hidden or inseparable from each other are review-required rather
   than estimated.
7. The local workbook is controlled by the application while monitoring is active; editing
   it externally during a run is unsupported.
8. There is one demo operator and no login or role management in v1.
9. English is used for machine-readable status values; the UI may show Chinese labels.
10. Processed FPS means frames passed through detection/tracking, not preview refresh rate.

## 5. Boundaries

### Always do

- Display which source, zone, SKU, location, task, analysis rate, and processing mode are active.
- Preserve source timestamps and order observations by those timestamps.
- Show whether quantity came from observed tracks or from operator correction.
- Keep inventory unchanged until an event is approved.
- Retain the original system result when a human edits an event.
- Stop automatic approval when the source is disconnected, tracking is ambiguous, or persistence fails.
- Make dropped frames and processing lag visible.

### Ask first

- Supporting more than one simultaneous source or zone.
- Adding a new source protocol, detection model format, or business event type.
- Changing event completion or auto-approval semantics.
- Adding external network access, user accounts, or a production WMS connection.
- Changing the workbook schema after implementation planning is approved.

### Never do

- Infer hidden item count solely from expected order quantity.
- Count the same confirmed track more than once in one direction.
- Mutate inventory for a rejected, incomplete, or review-required event.
- Hide model/source failures behind a stale preview.
- claim that the demo demonstrates full-warehouse or production accuracy.

## 6. Functional Acceptance Criteria

### AC-1: Video-file input [MUST]

Given a supported local video file selected by the operator
When the operator starts monitoring
Then the application plays the file in source timestamp order, analyzes frames at the
configured 3-5 FPS rate, and reaches end-of-file without treating end-of-file as a pick.

### AC-2: Local-camera input [MUST]

Given an available local camera selected by the operator
When the operator starts monitoring
Then the application displays a live preview, analyzes frames at the configured rate, and
reports a clear source error without changing inventory if the camera cannot be opened.

### AC-3: Network-stream input [MUST]

Given a valid RTSP or HTTP video-stream URL
When the operator starts monitoring
Then the application displays and analyzes the stream, and if the stream disconnects it
marks the source disconnected, pauses event finalization, and attempts bounded reconnection
without blocking the web interface.

### AC-4: Source switching [MUST]

Given monitoring is stopped
When the operator switches between file, camera, and network source types
Then the previous source is released, only the newly selected source becomes active, and no
frames from the previous source appear in the new session.

Given monitoring is active
When the operator requests a source switch
Then the application requires the current session to be stopped or explicitly discarded
before opening the new source.

### AC-5: Pick-zone calibration [MUST]

Given a visible source frame
When the operator draws valid shelf, interaction, exit, and directional counting regions
and saves the profile
Then the application reloads the same normalized regions for that source profile after a
restart and rejects self-intersecting or out-of-frame regions.

### AC-6: Zone and task readiness [MUST]

Given any required source, zone, SKU, location, or open task is missing
When the operator attempts to start monitoring
Then monitoring does not start and the interface identifies every missing prerequisite.

### AC-7: Configurable analysis rate [MUST]

Given an active source
When the operator selects 3, 4, or 5 analyzed frames per second
Then the measured analyzed-frame rate over a 30-second stable interval is within 15% of the
selected rate when the processing pipeline is capable, and overload is reported otherwise.

### AC-8: Non-blocking real-time behavior [MUST]

Given a live source produces frames faster than they can be analyzed
When the analyzer falls behind
Then stale unprocessed frames are dropped, queue depth remains bounded, preview and controls
remain responsive, and the most recently completed result is no more than 1.0 second behind
the newest captured frame at p95 under the baseline performance fixture.

### AC-9: Goods detection [MUST]

Given a supported good is visibly present in the configured interaction area
When a frame is analyzed
Then the interface displays its class, confidence, and bounding box, and detections below
the configured confidence threshold do not create confirmed tracks or counts.

### AC-10: Stable tracking [MUST]

Given a supported good remains visible across consecutive analyzed frames
When it moves within or across configured regions
Then it retains one stable track identifier unless tracking confidence is lost, and a lost
track is marked ambiguous rather than silently replaced and counted.

### AC-11: Pick-direction count [MUST]

Given a confirmed track starts on the shelf side of the counting boundary
When its anchor point crosses to the exit side and remains there for the configured
confirmation interval
Then the current event records one `PICK +1` action for that track.

### AC-12: Return-direction count [MUST]

Given a track previously recorded as picked remains associated with the active event
When it crosses from the exit side back into the shelf side and remains there for the
configured confirmation interval
Then the current event records one `RETURN -1` action and reduces the net quantity by one.

### AC-13: Crossing debounce and deduplication [MUST]

Given a track touches, jitters around, or briefly crosses the counting boundary
When it does not satisfy the directional confirmation interval
Then no action is recorded.

Given a track has one confirmed directional action
When subsequent frames show the same track on the same side
Then no duplicate action is recorded.

### AC-14: Event start [MUST]

Given the system is idle and monitoring a ready zone
When the first confirmed pick or return action occurs
Then a new active event starts at the source timestamp of that action and captures task,
zone, SKU, location, source-session, and configuration-version references.

### AC-15: Continuous-event grouping [MUST]

Given an active event has one or more actions
When additional picks or returns occur after a pause shorter than 10 seconds
Then all actions remain in the same event and net quantity is recalculated from the complete
action sequence.

### AC-16: Event completion [MUST]

Given an active event has no new confirmed action for 3 seconds
When the interaction region is stable for a further 2 seconds and no unresolved track is
crossing the boundary
Then the event is completed and submitted for reconciliation.

Given the interaction region does not become stable
When 10 seconds pass without a confirmed action
Then the event completes as review-required with reason `HARD_IDLE_TIMEOUT`.

### AC-17: Net quantity [MUST]

Given a completed event contains pick and return actions
When the event is reconciled
Then observed quantity equals confirmed picks minus confirmed returns, never falls below
zero, and is displayed with the ordered action timeline.

### AC-18: Task match [MUST]

Given a completed event has an observed SKU, source location, unit, and net quantity equal to
the selected open task
When every mandatory confidence and integrity check passes
Then the event is eligible for automatic approval.

### AC-19: Task mismatch [MUST]

Given observed SKU, location, unit, or net quantity differs from the selected task
When reconciliation runs
Then the event becomes review-required, every differing field is highlighted, and inventory
and task status remain unchanged.

### AC-20: Automatic approval policy [MUST]

Given a completed event matches its task
When aggregate confidence is at least 0.995, no track is ambiguous, no source discontinuity
overlaps the event, and persistence is writable
Then the application approves the event once, applies the inventory movement once, and
marks the task completed.

Given any automatic-approval prerequisite is false
When reconciliation runs
Then the event becomes review-required rather than being guessed or discarded.

### AC-21: Manual review [MUST]

Given an event is review-required
When the operator approves the observed result, edits SKU/quantity/unit/location with a
non-empty reason, or rejects the event
Then the application records the decision, operator identifier, timestamp, original result,
final result, and reason; only an approved final result may update inventory.

### AC-22: Idempotent inventory update [MUST]

Given an approved event has already been applied
When the same event is submitted or replayed again
Then inventory and task balances remain unchanged and the duplicate attempt is recorded.

### AC-23: Insufficient inventory [MUST]

Given approval would reduce a location below zero
When the application attempts to apply the event
Then the mutation is rejected, the event becomes review-required with reason
`INSUFFICIENT_INVENTORY`, and the original balance is preserved.

### AC-24: Workbook-backed task and inventory data [MUST]

Given a valid demo workbook is loaded
When the application starts
Then it validates required workbook structure and loads SKU, location, inventory, open task,
event, action, and audit information without requiring another database service.

### AC-25: Atomic workbook persistence [MUST]

Given an event or review decision must be persisted
When the workbook write succeeds
Then all affected business records are visible together after reopening the workbook.

Given the workbook is locked, corrupt, missing required structure, or cannot be replaced
atomically
When persistence is attempted
Then no partial business mutation is accepted, monitoring shows a blocking persistence
error, and automatic approval is disabled.

### AC-26: Restart recovery [MUST]

Given the application stopped after committing approved events
When it restarts with the same workbook
Then inventory, tasks, events, actions, decisions, and idempotency state are restored and
previous events are not applied again.

### AC-27: Event evidence [MUST]

Given an event starts and later completes
When evidence is available from the source
Then the event references frames or a clip covering at least 2 seconds before its first
action through 2 seconds after completion, and the operator can replay that evidence.

Given evidence cannot be retained
When reconciliation runs
Then the event is review-required with reason `EVIDENCE_UNAVAILABLE`.

### AC-28: Live monitoring interface [MUST]

Given monitoring is active
When frames and results arrive
Then one browser page shows source status, preview, configured regions, detections, track IDs,
directional actions, active-event state, net quantity, selected task, inventory balance,
analyzed FPS, dropped-frame count, queue/backlog status, and result latency without a full-page refresh.

### AC-29: Operator controls [MUST]

Given the application is running
When the operator uses start, pause, resume, stop, reset, approve, edit, reject, or evidence
replay controls
Then each control has a visible enabled/disabled state, reports completion or failure, and
does not produce an unintended inventory mutation.

### AC-30: Session reset [MUST]

Given the operator resets a demo scenario
When reset is confirmed
Then active workers stop, in-progress unapproved observations are discarded, configured
scenario workbook values are restored, and reset itself is recorded in the audit trail.

## 7. Error and Edge-Case Acceptance Criteria

### AC-E1: Unsupported or damaged video [MUST]

Given a selected file is unsupported or cannot be decoded
When the operator opens it
Then monitoring does not start, a specific source error is displayed, and no task or inventory
record changes.

### AC-E2: Stream disconnect during an event [MUST]

Given a network stream disconnects while an event is active
When frames stop arriving
Then the event is frozen, marked review-required with reason `SOURCE_INTERRUPTED`, and is not
automatically approved after reconnection.

### AC-E3: Overlapping or inseparable goods [MUST]

Given two or more goods cannot be independently detected or tracked through the boundary
When a count cannot be supported by distinct confirmed tracks
Then the system flags ambiguity and does not substitute the WMS expected quantity.

### AC-E4: Track loss near boundary [MUST]

Given a track is lost within the configured boundary uncertainty band
When its final direction cannot be confirmed
Then no directional count is committed for that track and the active event requires review.

### AC-E5: Out-of-order processing result [MUST]

Given parallel processing completes a later frame before an earlier frame
When tracking and event logic consume results
Then results are applied in source timestamp order or the late result is discarded according
to a visible policy; event action order must never be reversed.

### AC-E6: Browser disconnect [MUST]

Given the browser closes or loses its live-update connection
When backend monitoring continues
Then processing and persistence remain consistent, and reopening the page shows current
source and event state without replaying inventory operations.

### AC-E7: Negative or zero net event [MUST]

Given a completed action sequence has a net quantity of zero
When reconciliation runs
Then the event is retained as a no-op observation and does not mutate inventory.

Given returns would make the event net quantity negative
When the invalid action sequence is detected
Then the event becomes review-required and no inventory mutation occurs.

### AC-E8: Application shutdown [MUST]

Given capture or analysis is active
When the application receives a normal shutdown request
Then it stops accepting new frames, finishes or cancels in-flight work within 5 seconds,
closes the source, flushes committed audit records, and exits without corrupting the workbook.

## 8. Prioritized Optional Criteria

### AC-S1: Simultaneous visible goods [SHOULD]

Given up to three independently visible supported goods move through the interaction area
When they cross the boundary with separable tracks
Then each retains a distinct track ID and contributes its own directional action.

### AC-S2: Reference metrics export [SHOULD]

Given a monitoring session has ended
When the operator exports the session report
Then the report contains source duration, captured and analyzed frame counts, dropped-frame
ratio, mean and p95 latency, event counts, review reasons, and final task/inventory outcomes.

### AC-S3: Chinese interface labels [SHOULD]

Given the operator selects Chinese
When the page renders
Then user-facing labels, state descriptions, validation messages, and review reasons are
shown in Chinese while machine-readable values remain stable.

### AC-C1: Optional person-presence signal [COULD]

Given a supported person-presence detector is enabled
When a person enters or leaves the interaction and exit regions
Then presence may contribute to event-start and event-end confidence but cannot create item
counts by itself.

### AC-C2: Webcam setup wizard [COULD]

Given a local camera is selected
When the operator opens setup mode
Then the interface may guide camera positioning, lighting, and boundary drawing using live
quality indicators.

### AC-W1: Multi-camera tracking [WONT]

This version will not correlate identities or item tracks across multiple cameras. Reason:
the demo validates one controlled pick zone and one active source.

### AC-W2: Arbitrary SKU recognition [WONT]

This version will not recognize every warehouse SKU without prior configuration or model
support. Reason: v1 demonstrates event capture and counting, not open-world product mastery.

### AC-W3: Production WMS writes [WONT]

This version will not write to a customer production WMS. Reason: shadow-mode behavior must
be validated before external inventory mutation.

### AC-W4: Hidden-item estimation [WONT]

This version will not infer fully occluded item quantity from pallet geometry or expected
order quantity. Reason: unsupported inference would undermine auditability.

## 9. Non-Functional Requirements

### Performance

- Mandatory baseline: 1280x720 H.264 video or camera input, one source, one zone, configured
  4 analyzed FPS, one to three visible supported goods, no discrete GPU.
- Preview target: at least 10 displayed FPS when the source provides at least 10 FPS.
- Analysis target: selected 3-5 FPS within 15% over a 30-second stable interval when not overloaded.
- Result latency: no more than 1.0 second p95 from newest captured frame timestamp to visible
  completed analysis result on the baseline fixture.
- UI command acknowledgement: less than 250 ms p95 for controls that do not wait for source open/close.
- Memory: no monotonic growth above 100 MB across a 30-minute deterministic replay after warm-up.

### Reliability

- All processing queues are bounded and expose depth/drop counters.
- Source, inference, tracking, event, and persistence errors have distinct visible states.
- A worker failure cannot leave the page falsely showing `RUNNING`.
- One complete 30-minute replay must finish without an unhandled exception.

### Security and privacy

- Default access is loopback-only.
- Network-stream credentials are redacted everywhere outside runtime memory.
- Source paths and URLs are validated; local file access cannot escape allowed directories.
- No evidence or telemetry leaves the laptop automatically.

### Usability

- A first-time operator can select a prepared scenario and start monitoring in no more than
  five visible actions after opening the page.
- Every status must include text in addition to color.
- Destructive reset and rejection actions require confirmation.
- The video overlay must not obscure the current count or event state.

### Observability

- The application exposes source FPS, analyzed FPS, dropped frames, queue depth, result
  latency, source reconnect attempts, active event ID, and last persistence result.
- Every log record includes timestamp, severity, component, source session, and event ID when available.

### Portability

- The acceptance path runs on macOS on the target laptop.
- Source and business logic must not depend on a macOS-only API so a later Windows/Linux port remains possible.

## 10. Out of Scope

- Smart Gate functionality.
- Multiple simultaneous sources or pick zones.
- Face recognition, worker identification, attendance, or productivity scoring.
- Forklift localization and pallet movement tracking.
- Weight sensors, RFID, BLE, or other physical sensors.
- Training a production-quality detector inside the application.
- Cloud inference, remote fleet management, or remote evidence storage.
- Mobile UI and native desktop application packaging.
- Batch processing of an entire video faster than real time.
- Production retention, privacy, disaster recovery, or regulatory policy.

## 11. Demo Data and Ground Truth

The acceptance set must include timestamped annotations for at least these clips:

| Scenario | Ground truth |
|---|---|
| Walk-by without touching goods | no event |
| Pick one item | `PICK +1`, net 1 |
| Pick eight items continuously | eight picks, net 8 |
| Pick three, pause five seconds, pick five | one business event, net 8 |
| Pick three, return one | three picks, one return, net 2 |
| Task expects eight, observed seven | review-required, net 7 |
| Track lost at boundary | review-required, no guessed count |
| Video ends with no active interaction | no false pick |
| Stream interruption during active event | frozen review-required event |

Each annotation identifies source timestamps, object tracks when known, directional actions,
event boundaries, expected decision, and inventory effect.

## 12. Workbook Behavior Requirements

The workbook must visibly represent these business concepts, whether as sheets or equivalent
tables decided during Gate 2:

- SKU master data
- location master data
- inventory balances
- pick tasks
- source and zone profiles
- monitoring sessions
- events
- event actions
- review decisions
- audit records

Required workbook behavior:

1. A human can inspect the workbook after the application closes.
2. IDs, timestamps, quantities, units, statuses, reasons, and references remain explicit.
3. Original observed values and human-corrected values are both retained.
4. Approved event, task, and inventory changes are written as one logical transaction.
5. Reopening the workbook reconstructs current inventory and prevents duplicate event application.

## 13. Gate 1 Review Questions

The following are decisions, not unresolved requirements. Reviewers should explicitly accept
or amend them before planning:

1. Is one active source and one single-SKU zone sufficient for v1?
2. Is 4 FPS default, configurable from 3-5 FPS, the correct processing target?
3. Is p95 result latency of 1.0 second acceptable for the laptop demo?
4. Should automatic approval be enabled in the demo at 0.995, or should all events require a click?
5. Is `RTSP + HTTP video stream` sufficient for network input in v1?
6. Is review-required behavior acceptable for overlapping/fully occluded goods?
7. Is application-exclusive workbook access acceptable while monitoring runs?
8. Should Chinese UI labels be promoted from `[SHOULD]` to `[MUST]`?

## 14. Open Questions

- [RESOLVED] Database choice → Use one local Excel-compatible `.xlsx` workbook for v1.
- [RESOLVED] Frontend form → Browser-based local web application.
- [RESOLVED] Inputs → Video file, local camera, and network stream.
- [RESOLVED] Processing rate → Configurable 3-5 analyzed FPS, default 4 FPS.
- [RESOLVED] GPU requirement → No discrete GPU required for mandatory acceptance.
- [RESOLVED] Scope → Pick Zone only; Smart Gate remains a separate feature.
- [RESOLVED] Counting semantics → Track-confirmed directional actions and net pick-minus-return quantity.

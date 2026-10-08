# Technical Plan: Pick Zone Real-Time Counting Demo

Status: Approved at Gate 2 on 2026-10-08; contracts frozen
Version: 1.0
Last updated: 2026-10-08

## Spec Reference

Implements: `specs/pick-zone-demo/spec.md` v1.0  
Governed by: `constitution.md` v1.0.0

## 1. Architecture Overview

The application is one Python process with isolated long-running workers and a local web
server. A source worker continuously captures frames into a latest-frame buffer; a sampler
selects frames at 3-5 FPS; one ordered analysis worker runs detection, tracking, crossing,
and event-state updates; separate preview and persistence paths prevent browser or workbook
latency from blocking analysis. The browser receives an MJPEG preview plus ordered JSON
state updates. All business mutations are serialized through one application service and one
atomic workbook writer.

The v1 vision path uses a configurable YOLO-shaped ONNX detector adapter for a small,
customer-controlled set of item classes and a lightweight predictive IoU/centroid tracker.
Tests and recorded demo scenarios can substitute a deterministic replay detector without
changing tracking, counting, events, or WMS logic.

## 2. Target Environment and Locked Stack

### Verified host

| Property | Value |
|---|---|
| Machine | Apple Mac mini, M4, 10 CPU cores |
| Memory | 32 GB |
| Architecture | arm64 |
| OS | macOS 26.5.1 |
| Python | 3.12.13 |
| FFmpeg | 8.1.1 with VideoToolbox |

### Direct dependencies

| Package | Version | Role |
|---|---:|---|
| `fastapi` | 0.143.0 | HTTP and WebSocket server |
| `uvicorn` | 0.54.0 | ASGI runtime |
| `opencv-python` | 5.0.0.93 | capture, decode, geometry, overlay, JPEG encoding |
| `onnxruntime` | 1.30.0 | CPU model inference |
| `openpyxl` | 3.1.5 | workbook repository |
| `pydantic` | 2.13.5 | boundary validation and typed settings |
| `numpy` | 2.5.3 | frame/model arrays |
| `python-multipart` | 0.0.32 | video upload |
| `pytest` | 9.1.1 | tests |
| `pytest-asyncio` | 1.4.0 | async tests |
| `httpx` | 0.28.1 | API tests |
| `playwright` | 1.63.0 | browser tests |
| `ruff` | 0.16.10 | lint/format |

These versions were installed and imported together on the verified host. Transitive
dependencies will be captured in a generated lock file during setup.

## 3. Runtime Topology

```text
                         ┌───────────────────────┐
file / camera / RTSP ──▶ │ CaptureWorker         │
                         │ source clock + health │
                         └──────────┬────────────┘
                                    │ LatestFrameBuffer(size=2)
                    ┌───────────────┴────────────────┐
                    │                                │
                    ▼                                ▼
          PreviewWorker                         FrameSampler
          10 FPS target                         3-5 FPS
                    │                                │
                    │                                ▼
                    │                        OrderedAnalyzer
                    │                 detector → tracker → crossing
                    │                         → event machine
                    │                                │
                    └────────────┬───────────────────┘
                                 ▼
                         RuntimeStateStore
                       latest frame + metadata
                         │               │
                         ▼               ▼
                  MJPEG endpoint    WebSocket state

 OrderedAnalyzer ──▶ BusinessEventService ──▶ PersistenceQueue(size=100)
                                                │
                                                ▼
                                          WorkbookWriter
                                     temp save → fsync → replace
```

### Ordering rule

Capture and presentation are parallel. Detection, track association, crossing updates, and
event state transitions for one source are intentionally sequential. Every analyzed frame
has `(session_id, sequence, source_timestamp_ms, captured_monotonic_ns)`. Results with a
sequence not greater than the last applied sequence are discarded and recorded as late.

### Backpressure rule

- `LatestFrameBuffer` holds at most two frames: one being read and the newest available.
- Sampler always selects the newest eligible frame for the next scheduled analysis slot.
- Preview uses the newest source frame and latest completed metadata; it never waits for inference.
- Persistence uses a bounded queue. When unavailable or full, automatic approval is disabled
  and the active/completed event remains review-required in memory.

## 4. Component Breakdown

### 4.1 Application lifecycle and settings

- **Responsibility:** validate paths/settings, build the component graph, start/stop workers,
  expose health, and complete orderly shutdown within five seconds.
- **Location:** `code/pick-zone-demo/app/core/`
- **Accepts:** environment, CLI arguments, saved profiles.
- **Returns:** initialized `ApplicationContext` and lifecycle status.
- **AC Coverage:** AC-6, AC-E8; security, reliability, and portability NFRs.

### 4.2 Source adapters

- **Responsibility:** implement the common `VideoSource` contract for uploaded/local files,
  camera indexes, and RTSP/HTTP streams; normalize timestamps; report source health; reconnect
  network streams with bounded exponential backoff.
- **Location:** `app/video/sources.py`, `app/video/capture.py`
- **Accepts:** validated source configuration.
- **Returns:** ordered `FrameEnvelope` values and `SourceHealth` updates.
- **AC Coverage:** AC-1 through AC-4, AC-E1, AC-E2, AC-E8.

### 4.3 Latest-frame buffer and sampler

- **Responsibility:** decouple capture from analysis, select newest frames at 3-5 FPS, count
  dropped frames, and prevent latency growth.
- **Location:** `app/video/buffer.py`, `app/video/sampler.py`
- **Accepts:** captured frame envelopes and requested analysis rate.
- **Returns:** sampled frame envelopes and pipeline metrics.
- **AC Coverage:** AC-7, AC-8, AC-E5; performance and observability NFRs.

### 4.4 Zone profile service

- **Responsibility:** validate normalized polygons/line, serialize profiles, map tracks to
  shelf/interaction/exit regions, and compute signed line sides and uncertainty band.
- **Location:** `app/domain/zones.py`, `app/services/profile_service.py`
- **Accepts:** browser calibration commands and source dimensions.
- **Returns:** versioned `ZoneProfile`.
- **AC Coverage:** AC-5, AC-6, AC-30.

### 4.5 Detector adapter

- **Responsibility:** preprocess a sampled frame, invoke the configured ONNX model, apply
  confidence/class filtering and non-maximum suppression, and return image-space detections.
- **Location:** `app/vision/detector.py`, `app/vision/onnx_detector.py`
- **Accepts:** frame, active class allow-list, threshold, model manifest.
- **Returns:** ordered `Detection[]`.
- **AC Coverage:** AC-9, AC-E3; AC-S1.

The runtime adapter supports standard YOLO-style outputs through a model manifest that defines
input size, layout, normalization, class names, output tensor mapping, and license metadata.
No model download occurs at runtime. A deterministic `ReplayDetector` reads replay observations
for tests and stable recorded demonstrations.

### 4.6 Lightweight tracker

- **Responsibility:** maintain stable track IDs using predicted centroid/velocity, IoU, class
  consistency, and bounded missed-frame age; mark tracks ambiguous around the boundary.
- **Location:** `app/vision/tracker.py`
- **Accepts:** timestamped detections in sequence.
- **Returns:** `TrackObservation[]` and track lifecycle events.
- **AC Coverage:** AC-10, AC-E3, AC-E4, AC-E5; AC-S1.

The tracker uses deterministic greedy association because v1 has at most three visible items.
It does not introduce SciPy or a general tracking framework. Unmatched detections start
tentative tracks; tracks require two observations before confirmation; tracks expire after a
configurable missed duration. Track IDs are monotonic within a source session.

### 4.7 Crossing engine

- **Responsibility:** derive pick/return actions from confirmed track transitions across the
  directional line, enforce stable-side debounce, and prevent duplicate actions.
- **Location:** `app/domain/crossing.py`
- **Accepts:** confirmed track histories and zone profile.
- **Returns:** zero or more immutable `DirectionalAction` values.
- **AC Coverage:** AC-11 through AC-13, AC-E4, AC-E7.

Default confirmation is two consecutive analyzed observations on the destination side and
outside the uncertainty band. The action ledger key is `(event_id, track_id, direction,
transition_index)`; a track cannot emit the same transition twice without first completing
the reverse transition.

### 4.8 Event state machine

- **Responsibility:** group actions into one event, calculate net quantity, apply quiet,
  stable, hard-idle, and interrupted-source transitions, and retain ordered action evidence.
- **Location:** `app/domain/event_machine.py`
- **Accepts:** source health, motion stability, actions, timestamps, unresolved tracks.
- **Returns:** `EventSnapshot` transitions.
- **AC Coverage:** AC-14 through AC-17, AC-E2, AC-E7.

States: `IDLE`, `ACTIVE`, `SETTLING`, `COMPLETED`, `REVIEW_REQUIRED`, `APPROVED`,
`REJECTED`. Timers use source timestamps for files/replay and monotonic elapsed time for live
source health. Tests inject a clock and never sleep.

### 4.9 Task reconciliation and inventory service

- **Responsibility:** compare completed observations with task fields, calculate aggregate
  decision eligibility, apply manual decisions, enforce non-negative inventory, idempotency,
  and task completion.
- **Location:** `app/services/reconciliation.py`, `app/services/inventory_service.py`
- **Accepts:** completed event, selected task, current inventory, review command.
- **Returns:** decision plus one atomic `WorkbookMutation`.
- **AC Coverage:** AC-18 through AC-23, AC-E7.

Aggregate confidence is the minimum of mandatory evidence confidences, not an average. A
perfect task match never raises vision confidence. Auto-approval additionally requires the
event-integrity flags specified by AC-20.

### 4.10 Workbook repository and writer

- **Responsibility:** validate schema, load a consistent snapshot, queue serialized mutations,
  write all affected sheets to a temporary workbook, fsync, atomically replace, retain one
  backup, and publish persistence status.
- **Location:** `app/persistence/workbook.py`, `app/persistence/writer.py`
- **Accepts:** validated workbook path and `WorkbookMutation` commands.
- **Returns:** snapshots, commit receipts, or typed failures.
- **AC Coverage:** AC-22, AC-24 through AC-26, AC-30, AC-E8.

### 4.11 Evidence manager

- **Responsibility:** keep a bounded encoded-frame ring covering at least two pre-event
  seconds, finalize an MP4 evidence clip after event completion, and return a relative path.
- **Location:** `app/video/evidence.py`
- **Accepts:** source frames, event boundaries, storage policy.
- **Returns:** evidence manifest/path or typed failure.
- **AC Coverage:** AC-27, AC-E2.

Evidence encoding uses the installed FFmpeg executable in a controlled subprocess. No shell
interpolation is used.

### 4.12 Runtime state and preview renderer

- **Responsibility:** maintain an immutable latest runtime snapshot, draw zones/detections/
  tracks/actions, encode the newest annotated JPEG, and expose metrics.
- **Location:** `app/services/runtime_state.py`, `app/vision/overlay.py`
- **Accepts:** latest source frame plus latest ordered analysis/event state.
- **Returns:** MJPEG frames and JSON snapshots.
- **AC Coverage:** AC-8 through AC-10, AC-28, performance/usability/observability NFRs.

### 4.13 Local web API and UI

- **Responsibility:** serve one dashboard, source/profile/task setup, lifecycle controls,
  event review, evidence playback, status, and live updates.
- **Location:** `app/api/`, `app/static/`
- **Accepts:** validated HTTP commands and WebSocket connections.
- **Returns:** contract-defined JSON, MJPEG/evidence media, and static assets.
- **AC Coverage:** AC-1 through AC-6, AC-21, AC-27 through AC-30, AC-E1, AC-E6;
  AC-S2, AC-S3, AC-C2.

### 4.14 Session metrics exporter

- **Responsibility:** aggregate capture/analysis/drop/latency/event/review metrics and export
  a workbook sheet or CSV report after a session.
- **Location:** `app/services/metrics.py`
- **Accepts:** session metrics and event summaries.
- **Returns:** report path.
- **AC Coverage:** AC-S2 and observability NFRs.

## 5. Source Processing Details

### 5.1 File source

- Source timestamps derive from frame position/FPS, corrected to remain monotonic.
- Playback is paced against monotonic wall time at 1x speed.
- Pause freezes playback and event timers; resume continues source time.
- EOF transitions source to `ENDED` and does not synthesize an event action.

### 5.2 Camera source

- Camera indexes are probed only on operator request.
- Capture uses backend defaults first, then requests 1280x720.
- Unavailable camera yields a typed open error and no worker starts.

### 5.3 Network source

- URL schemes allowed: `rtsp`, `rtsps`, `http`, `https`.
- RTSP prefers TCP transport for demo stability.
- Open/read timeouts are finite and configurable.
- Reconnect schedule: 1, 2, 4, 8, then 10 seconds capped; status and attempts are visible.
- A disconnect overlapping an active event adds `SOURCE_INTERRUPTED` permanently; reconnect
  begins a new source continuity segment and cannot restore auto-approval for that event.

## 6. Concurrency and Shutdown

| Worker | Concurrency | Queue/buffer | Shutdown behavior |
|---|---|---|---|
| Capture | one thread | latest frame, capacity 2 | release source immediately |
| Preview | one thread/task | reads latest snapshots | stop after current JPEG |
| Sampler/analyzer | one thread | capacity 1 sampled frame | finish current inference or timeout |
| Evidence | one worker | bounded encoded ring | finalize committed clips only |
| Workbook | one thread | mutation capacity 100 | flush committed receipt, reject remainder |
| Web server | async loop | per-client latest state | close clients after worker stop |

No analysis stage for a single source runs concurrently with another analysis stage; this
protects tracker ordering and avoids ONNX session contention. Parallelism exists around the
ordered analyzer, where it provides value without changing event semantics.

## 7. Model and Demo-Data Strategy

### 7.1 Runtime model contract

Each model has a `model-manifest.json` containing:

- model ID, version, and SHA-256
- license and source
- input shape/layout/type and normalization
- output tensor mapping
- class IDs/names and supported demo SKUs/classes
- confidence and NMS defaults
- validation clip set and last measured metrics

The application refuses a model whose file hash or manifest is missing or mismatched.

### 7.2 Initial detector artifact

The first working slice may use `ReplayDetector` to validate all event/business behavior.
The live acceptance slice requires a small ONNX detector trained or exported outside the
runtime for staged goods. Model preparation is a development task with license recording and
fixture-level evaluation; the web application does not train models.

### 7.3 Ground-truth replay

Every acceptance clip has a sidecar file with source timestamps, detections or tracks,
directional actions, event boundaries, and expected business result. Replay mode drives the
same tracker/crossing/event/business pipeline wherever possible; only detector output is
substituted.

## 8. Web Delivery Design

- `GET /` serves the monitoring dashboard.
- MJPEG is used for preview because it is locally robust and avoids a browser video-codec
  pipeline in v1.
- `WS /api/v1/live` sends typed runtime snapshots. Slow clients retain only the newest snapshot.
- Commands are JSON HTTP requests with request IDs and typed errors.
- Browser reconnect fetches a full snapshot before resuming live updates.
- Evidence uses HTTP byte-range responses for browser replay controls.

## 9. Persistence Transaction Design

The workbook repository loads workbook rows into typed in-memory maps. A business mutation
contains complete expected preconditions and effects:

```text
event decision transition
event observed/final values
review row when applicable
action rows
source-location inventory delta
destination-location inventory delta
task status transition
audit row
idempotency receipt
```

The writer verifies preconditions against its latest committed snapshot, applies all changes
in memory, writes a complete temporary workbook in the same directory, flushes and fsyncs it,
then atomically replaces the main workbook. Failure before replacement leaves the previous
file untouched. One timestamped backup is retained.

## 10. API and Public Contracts

Contracts are defined in:

- `contracts/source-monitoring-api.md`
- `contracts/profiles-tasks-api.md`
- `contracts/events-review-api.md`
- `contracts/live-stream-contract.md`
- `contracts/vision-domain-contract.md`
- `contracts/workbook-contract.md`

After Gate 2 approval these files are frozen until implementation validation or an approved
SDD amendment.

## 11. AC Coverage Map

| AC | Component(s) | Contract(s) |
|---|---|---|
| AC-1, AC-2, AC-3, AC-4 | Source adapters; Web API/UI | source-monitoring-api; live-stream |
| AC-5 | Zone profile service; Web API/UI | profiles-tasks-api; workbook |
| AC-6 | Lifecycle/settings; Profile/task service | source-monitoring-api; profiles-tasks-api |
| AC-7, AC-8 | Buffer/sampler; Runtime state | source-monitoring-api; live-stream |
| AC-9 | Detector adapter; Preview renderer | vision-domain; live-stream |
| AC-10 | Tracker; Preview renderer | vision-domain; live-stream |
| AC-11, AC-12, AC-13 | Crossing engine | vision-domain |
| AC-14, AC-15, AC-16, AC-17 | Event state machine | vision-domain; live-stream |
| AC-18, AC-19, AC-20 | Reconciliation/inventory service | events-review-api; workbook |
| AC-21 | Reconciliation service; Web API/UI | events-review-api; workbook |
| AC-22, AC-23 | Inventory service; Workbook writer | events-review-api; workbook |
| AC-24, AC-25, AC-26 | Workbook repository/writer | workbook |
| AC-27 | Evidence manager; Web API/UI | events-review-api; live-stream |
| AC-28, AC-29 | Runtime state; Web API/UI | source-monitoring-api; live-stream; events-review-api |
| AC-30 | Lifecycle; Profile service; Workbook writer | source-monitoring-api; workbook |
| AC-E1 | File source; Web API/UI | source-monitoring-api |
| AC-E2 | Network source; Event state; Evidence | source-monitoring-api; vision-domain |
| AC-E3, AC-E4 | Detector/tracker/crossing | vision-domain |
| AC-E5 | Sampler/analyzer | vision-domain; live-stream |
| AC-E6 | Runtime state; Web API/UI | live-stream |
| AC-E7 | Event state; Reconciliation | vision-domain; events-review-api |
| AC-E8 | Lifecycle; all workers; Workbook writer | source-monitoring-api; workbook |
| AC-S1 | Detector/tracker/crossing | vision-domain |
| AC-S2 | Metrics exporter | source-monitoring-api |
| AC-S3 | Web UI | source-monitoring-api |
| AC-C1 | Optional presence adapter | vision-domain |
| AC-C2 | Web UI/profile service | profiles-tasks-api |

## 12. Test Strategy

### Unit tests

- Zone geometry and normalized-coordinate validation.
- Sampler scheduling, dropping, metrics, and out-of-order protection.
- Detector post-processing with synthetic tensors.
- Tracker confirmation, association, expiration, and ambiguity.
- Crossing debounce, pick, return, and duplicate suppression.
- Event state machine with injected timestamps.
- Reconciliation, confidence gates, non-negative inventory, and idempotency.
- Workbook row validation and mutation preconditions.

### Integration tests

- File source through replay detector to approved event and workbook mutation.
- Camera adapter with simulated device frames and open failure.
- Network adapter with local synthetic stream, disconnect, and reconnect.
- Atomic workbook save failure/locked-file behavior.
- Evidence pre/post roll and unavailable evidence handling.
- API commands and WebSocket snapshots against a running test server.

### Browser tests

- Prepared scenario starts in five or fewer visible actions.
- Live status updates without page reload.
- Review edit requires a reason and preserves original values.
- Reset confirmation and restored workbook state.
- Text states are present independent of color.

### Performance test

A 30-minute deterministic 1280x720 replay on the target M4 machine records preview FPS,
analysis FPS, drop ratio, p50/p95 latency, queue depths, CPU/memory, and worker failures. The
test uses a small ONNX fixture model first; the final live detector repeats the benchmark.

## 13. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Demo goods are not detectable by a generic model | High | High | Prepare a licensed custom ONNX artifact for staged goods; retain replay mode for business-flow work |
| At 3-5 FPS a fast object crosses too far for IoU matching | Medium | High | Add centroid/velocity gating, enlarge search by elapsed time, require review near boundary on loss |
| Person and carried item merge visually | High | High | Choose camera angle and visible goods; train on carried examples; ambiguity never auto-approves |
| RTSP backend blocks during disconnect | Medium | High | finite timeouts, isolated capture thread, bounded reconnect, integration stream test |
| XLSX atomic replace fails while externally open | Medium | High | exclusive-access warning, preflight writability, blocking state, no commit before receipt |
| Preview encoding consumes analysis CPU | Medium | Medium | cap preview at 10 FPS and scale frames; independent worker drops stale work |
| Out-of-order work corrupts track state | Low | High | one ordered analyzer, sequence checks, explicit late-result test |
| Evidence encoding stalls | Medium | Medium | bounded queue/subprocess timeout; review required if evidence unavailable |
| Workbook grows and saves become slow | Medium | Medium | demo retention cap, evidence outside workbook, measure save latency |
| Model or weights cannot be used commercially | Medium | High | model-manifest license gate; no unknown/non-commercial artifact |
| Auto-approval appears to prove production accuracy | Medium | High | UI labels demo policy; report accuracy, coverage, and review rate separately |

## 14. Technical Out of Scope

- Multiple active sources, multi-camera identity, distributed workers, or horizontal scaling.
- Database server, message broker, Docker/Kubernetes, cloud storage, or authentication.
- Model training UI, automatic model download, open-world SKU discovery, or VLM counting.
- GPU-specific runtimes, production WMS connectors, and physical sensor integration.
- Smart Gate behavior or cross-zone logistics-unit tracking.

## 15. Implementation Slices

The task phase should preserve these vertical slices:

1. **Deterministic core:** zones, replay observations, tracking, crossing, event state,
   reconciliation, and in-memory tests.
2. **Workbook transaction:** schema, snapshot, idempotent mutation, atomic persistence.
3. **File-source dashboard:** file input, sampler, annotated preview, live state, controls.
4. **Review workflow:** event list, evidence, approve/edit/reject, inventory/task updates.
5. **Live sources:** camera and network adapters, reconnect and interruption behavior.
6. **Live detector:** ONNX adapter, model manifest, staged-goods artifact, performance tuning.
7. **Acceptance hardening:** all fixtures, 30-minute benchmark, browser tests, documentation.

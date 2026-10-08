# Task List: Pick Zone Real-Time Counting Demo

Status: Approved at Gate 3 on 2026-10-08; implementation in progress
Version: 1.0
Last updated: 2026-10-08

## Plan Reference

Implements: `specs/pick-zone-demo/plan.md` v1.0  
Requirements: `specs/pick-zone-demo/spec.md` v1.0  
Data model: `specs/pick-zone-demo/data-model.md` v1.0  
Frozen interfaces: `specs/pick-zone-demo/contracts/`

## Execution Rules

1. Execute tasks in dependency order. A `[P]` task may run concurrently only with other
   ready `[P]` tasks that do not modify the same file.
2. Each implementation task starts only after its immediately preceding test task has been
   written and observed failing for the intended reason.
3. A task may change only the files named in that task. Generated lock metadata owned by the
   package manager is the sole exception.
4. Frozen contract files cannot be changed during implementation. A required contract change
   stops work and returns the feature to Gate 2 through an explicit amendment.
5. Every implementation task ends with its focused tests, the relevant broader test subset,
   and `ruff` checks passing before the task is marked complete.
6. Optional AC-S1 and AC-S2 are included. AC-S3 and both COULD criteria remain deferred unless
   all MUST criteria pass with schedule remaining.

## Tasks

### Foundation

- [x] **TASK-001** [S] Write bootstrap, settings, and dependency smoke tests
  - Creates: `code/pick-zone-demo/tests/test_bootstrap.py`
  - Tests: AC-6, AC-E8; portability and security NFRs
  - Verifies: Python 3.12 guard, required settings validation, loopback default, writable data
    paths, and dependency imports
  - Depends on: none

- [x] **TASK-002** [S] Implement the Python package bootstrap and validated settings
  - Creates: `code/pick-zone-demo/pyproject.toml`,
    `code/pick-zone-demo/app/__init__.py`, `code/pick-zone-demo/app/core/settings.py`
  - Contracts: `contracts/source-monitoring-api.md`, `contracts/workbook-contract.md`
  - Plan: sections 2 and 4.1
  - Satisfies: AC-6, AC-E8; portability and security NFRs
  - Depends on: TASK-001

- [x] **TASK-003** [M] [P] Write domain-value and zone-geometry tests
  - Creates: `code/pick-zone-demo/tests/domain/test_zones.py`
  - Tests: AC-5, AC-6, AC-30
  - Verifies: normalized coordinates, required region names, self-intersection, out-of-frame
    rejection, line-side calculation, uncertainty band, and profile round trip
  - Depends on: TASK-002

- [x] **TASK-004** [M] [P] Implement immutable domain values and zone geometry
  - Creates: `code/pick-zone-demo/app/domain/models.py`,
    `code/pick-zone-demo/app/domain/zones.py`
  - Contracts: `contracts/vision-domain-contract.md`, `contracts/profiles-tasks-api.md`
  - Plan: section 4.4
  - Satisfies: AC-5, AC-6, AC-30
  - Depends on: TASK-003

- [x] **TASK-005** [S] [P] Write replay-detector and detector-contract tests
  - Creates: `code/pick-zone-demo/tests/vision/test_detector_contract.py`
  - Tests: AC-9, AC-E3; AC-S1
  - Verifies: class allow-list, confidence filtering, deterministic sequence replay, and
    distinct observations for up to three separable goods
  - Depends on: TASK-002

- [x] **TASK-006** [M] [P] Implement the detector interface and deterministic ReplayDetector
  - Creates: `code/pick-zone-demo/app/vision/detector.py`,
    `code/pick-zone-demo/app/vision/replay_detector.py`
  - Contracts: `contracts/vision-domain-contract.md`
  - Plan: sections 4.5 and 7.3
  - Satisfies: AC-9, AC-E3; AC-S1
  - Depends on: TASK-005

### Ordered Vision And Event Core

- [x] **TASK-007** [M] Write lightweight-tracker tests
  - Creates: `code/pick-zone-demo/tests/vision/test_tracker.py`
  - Tests: AC-10, AC-E3, AC-E4, AC-E5; AC-S1
  - Verifies: tentative/confirmed lifecycle, stable monotonic IDs, velocity/centroid and IoU
    association, class consistency, ambiguity, expiration, and ordered updates
  - Depends on: TASK-004, TASK-006

- [x] **TASK-008** [M] Implement the deterministic lightweight tracker
  - Creates: `code/pick-zone-demo/app/vision/tracker.py`
  - Contracts: `contracts/vision-domain-contract.md`
  - Plan: section 4.6
  - Satisfies: AC-10, AC-E3, AC-E4, AC-E5; AC-S1
  - Depends on: TASK-007

- [x] **TASK-009** [M] Write directional-crossing tests
  - Creates: `code/pick-zone-demo/tests/domain/test_crossing.py`
  - Tests: AC-11, AC-12, AC-13, AC-E4, AC-E7
  - Verifies: two-observation confirmation, pick and return directions, jitter suppression,
    duplicate suppression, reverse-transition rearming, and lost-near-line ambiguity
  - Depends on: TASK-004, TASK-008

- [x] **TASK-010** [M] Implement directional crossing and action-ledger logic
  - Creates: `code/pick-zone-demo/app/domain/crossing.py`
  - Contracts: `contracts/vision-domain-contract.md`
  - Plan: section 4.7
  - Satisfies: AC-11, AC-12, AC-13, AC-E4, AC-E7
  - Depends on: TASK-009

- [x] **TASK-011** [M] Write event-state-machine tests with an injected clock
  - Creates: `code/pick-zone-demo/tests/domain/test_event_machine.py`
  - Tests: AC-14, AC-15, AC-16, AC-17, AC-E2, AC-E5, AC-E7
  - Verifies: first-action start, sub-10-second grouping, quiet/stable completion,
    hard-idle review, source interruption, ordered actions, zero net, and negative net
  - Depends on: TASK-010

- [x] **TASK-012** [M] Implement the event state machine
  - Creates: `code/pick-zone-demo/app/domain/event_machine.py`
  - Contracts: `contracts/vision-domain-contract.md`, `contracts/live-stream-contract.md`
  - Plan: section 4.8
  - Satisfies: AC-14, AC-15, AC-16, AC-17, AC-E2, AC-E5, AC-E7
  - Depends on: TASK-011

- [x] **TASK-013** [M] Write task-reconciliation and inventory-policy tests
  - Creates: `code/pick-zone-demo/tests/services/test_reconciliation.py`
  - Tests: AC-18, AC-19, AC-20, AC-21, AC-22, AC-23, AC-E7
  - Verifies: exact task match, field-level mismatch, minimum confidence aggregation, all
    auto-approval gates, manual edit reason, rejection, idempotency, zero net, negative net,
    and insufficient inventory
  - Depends on: TASK-012

- [x] **TASK-014** [M] Implement reconciliation, review decisions, and workbook mutations
  - Creates: `code/pick-zone-demo/app/domain/mutations.py`,
    `code/pick-zone-demo/app/services/reconciliation.py`,
    `code/pick-zone-demo/app/services/inventory_service.py`
  - Contracts: `contracts/events-review-api.md`, `contracts/workbook-contract.md`
  - Plan: sections 4.9 and 9
  - Satisfies: AC-18 through AC-23, AC-E7
  - Depends on: TASK-013

### Persistence And Concurrent Video Pipeline

- [x] **TASK-015** [M] [P] Write workbook schema, snapshot, and recovery tests
  - Creates: `code/pick-zone-demo/tests/persistence/test_workbook.py`
  - Tests: AC-5, AC-24, AC-26, AC-30
  - Verifies: exact sheets/headers, typed row validation, logical indexes, profile loading,
    prior event/idempotency recovery, schema-version rejection, and scenario snapshot restore
  - Depends on: TASK-014

- [x] **TASK-016** [M] [P] Implement workbook schema validation and snapshot repository
  - Creates: `code/pick-zone-demo/app/persistence/schema.py`,
    `code/pick-zone-demo/app/persistence/workbook.py`
  - Contracts: `contracts/workbook-contract.md`, `contracts/profiles-tasks-api.md`
  - Plan: sections 4.10 and 9; `data-model.md` sections 2 through 6
  - Satisfies: AC-5, AC-24, AC-26, AC-30
  - Depends on: TASK-015

- [x] **TASK-017** [M] Write atomic-workbook-writer tests
  - Creates: `code/pick-zone-demo/tests/persistence/test_writer.py`
  - Tests: AC-20, AC-21, AC-22, AC-23, AC-25, AC-26, AC-E8
  - Verifies: one serialized writer, precondition checks, duplicate receipts, all-sheet commit,
    temp-file fsync/reopen/replace, backup, simulated lock/replace failure, and shutdown flush
  - Depends on: TASK-016

- [x] **TASK-018** [M] Implement the serialized atomic workbook writer
  - Creates: `code/pick-zone-demo/app/persistence/writer.py`
  - Contracts: `contracts/workbook-contract.md`, `contracts/events-review-api.md`
  - Plan: sections 4.10, 6, and 9
  - Satisfies: AC-20 through AC-26, AC-E8
  - Depends on: TASK-017

- [x] **TASK-019** [M] [P] Write latest-frame-buffer and sampler tests
  - Creates: `code/pick-zone-demo/tests/video/test_sampler.py`
  - Tests: AC-7, AC-8, AC-E5, AC-E8
  - Verifies: capacity two, latest-frame wins, 3/4/5 FPS schedules, overload metrics, stale
    dropping, p95 latency calculation, sequence guard, and prompt cancellation
  - Depends on: TASK-014

- [x] **TASK-020** [M] [P] Implement the bounded frame buffer and sampler
  - Creates: `code/pick-zone-demo/app/video/buffer.py`,
    `code/pick-zone-demo/app/video/sampler.py`
  - Contracts: `contracts/vision-domain-contract.md`, `contracts/live-stream-contract.md`
  - Plan: sections 3, 4.3, and 6
  - Satisfies: AC-7, AC-8, AC-E5, AC-E8
  - Depends on: TASK-019

- [x] **TASK-021** [M] [P] Write file-source and camera-source adapter tests
  - Creates: `code/pick-zone-demo/tests/video/test_local_sources.py`
  - Tests: AC-1, AC-2, AC-4, AC-E1, AC-E8
  - Verifies: monotonic file timestamps, 1x pacing, pause/resume, EOF behavior, damaged media,
    camera probe/open failure, source release, and session isolation
  - Depends on: TASK-020

- [x] **TASK-022** [M] [P] Implement file and local-camera sources plus capture worker
  - Creates: `code/pick-zone-demo/app/video/sources.py`,
    `code/pick-zone-demo/app/video/capture.py`
  - Contracts: `contracts/source-monitoring-api.md`, `contracts/vision-domain-contract.md`
  - Plan: sections 4.2, 5.1, 5.2, and 6
  - Satisfies: AC-1, AC-2, AC-4, AC-E1, AC-E8
  - Depends on: TASK-021

- [x] **TASK-023** [M] Write network-stream reconnect tests
  - Creates: `code/pick-zone-demo/tests/video/test_network_source.py`
  - Tests: AC-3, AC-4, AC-E2, AC-E8
  - Verifies: scheme validation, finite open/read timeout, bounded 1/2/4/8/10-second backoff,
    health updates, active-event interruption, continuity segment change, and release
  - Depends on: TASK-022

- [x] **TASK-024** [M] Implement RTSP/HTTP stream capture and bounded reconnection
  - Creates: `code/pick-zone-demo/app/video/network_source.py`
  - Contracts: `contracts/source-monitoring-api.md`, `contracts/vision-domain-contract.md`
  - Plan: sections 4.2 and 5.3
  - Satisfies: AC-3, AC-4, AC-E2, AC-E8
  - Depends on: TASK-023

- [x] **TASK-025** [M] [P] Write model-manifest and ONNX post-processing tests
  - Creates: `code/pick-zone-demo/tests/vision/test_onnx_detector.py`
  - Tests: AC-9, AC-E3; AC-S1
  - Verifies: SHA-256/license gate, input layouts and normalization, output mapping, confidence
    filtering, NMS, class allow-list, deterministic ordering, and malformed model rejection
  - Depends on: TASK-006

- [x] **TASK-026** [M] [P] Implement manifest validation and YOLO-shaped ONNX detector
  - Creates: `code/pick-zone-demo/app/vision/model_manifest.py`,
    `code/pick-zone-demo/app/vision/onnx_detector.py`
  - Contracts: `contracts/vision-domain-contract.md`
  - Plan: sections 4.5, 7.1, and 7.2
  - Satisfies: AC-9, AC-E3; AC-S1
  - Depends on: TASK-025

- [ ] **TASK-027** [M] [P] Write event-evidence retention tests
  - Creates: `code/pick-zone-demo/tests/video/test_evidence.py`
  - Tests: AC-27, AC-E2, AC-E8
  - Verifies: bounded encoded ring, two-second pre/post roll, controlled FFmpeg arguments,
    finalization timeout, relative manifest path, missing evidence, and shutdown behavior
  - Depends on: TASK-012

- [ ] **TASK-028** [M] [P] Implement bounded evidence capture and clip finalization
  - Creates: `code/pick-zone-demo/app/video/evidence.py`
  - Contracts: `contracts/events-review-api.md`, `contracts/live-stream-contract.md`
  - Plan: sections 4.11 and 6
  - Satisfies: AC-27, AC-E2, AC-E8
  - Depends on: TASK-027

### Runtime Composition And APIs

- [ ] **TASK-029** [M] [P] Write runtime-state and preview-overlay tests
  - Creates: `code/pick-zone-demo/tests/services/test_runtime_state.py`
  - Tests: AC-8, AC-9, AC-10, AC-17, AC-28, AC-E5, AC-E6
  - Verifies: immutable snapshots, source sequence rejection, latest-only publication, required
    metrics/state fields, zone/detection/track/action drawing, and JPEG generation
  - Depends on: TASK-008, TASK-012, TASK-020

- [ ] **TASK-030** [M] [P] Implement runtime state store and annotated preview renderer
  - Creates: `code/pick-zone-demo/app/services/runtime_state.py`,
    `code/pick-zone-demo/app/vision/overlay.py`
  - Contracts: `contracts/live-stream-contract.md`, `contracts/vision-domain-contract.md`
  - Plan: section 4.12
  - Satisfies: AC-8, AC-9, AC-10, AC-17, AC-28, AC-E5, AC-E6
  - Depends on: TASK-029

- [ ] **TASK-031** [M] Write application-context and worker-lifecycle tests
  - Creates: `code/pick-zone-demo/tests/core/test_lifecycle.py`
  - Tests: AC-4, AC-6, AC-8, AC-29, AC-30, AC-E8
  - Verifies: prerequisite aggregation, worker graph, start/pause/resume/stop/reset transitions,
    source replacement, bounded queues, persistence blocking, and shutdown within five seconds
  - Depends on: TASK-018, TASK-022, TASK-024, TASK-026, TASK-028, TASK-030

- [ ] **TASK-032** [M] Implement application context, ordered analyzer, and lifecycle service
  - Creates: `code/pick-zone-demo/app/core/context.py`,
    `code/pick-zone-demo/app/services/orchestrator.py`,
    `code/pick-zone-demo/app/core/lifecycle.py`
  - Contracts: `contracts/source-monitoring-api.md`, `contracts/vision-domain-contract.md`,
    `contracts/workbook-contract.md`
  - Plan: sections 3, 4.1, 6, and 9
  - Satisfies: AC-4, AC-6, AC-8, AC-29, AC-30, AC-E8
  - Depends on: TASK-031

- [ ] **TASK-033** [M] Write source/monitoring HTTP API tests
  - Creates: `code/pick-zone-demo/tests/api/test_source_monitoring.py`
  - Tests: AC-1, AC-2, AC-3, AC-4, AC-6, AC-7, AC-29, AC-30, AC-E1, AC-E8
  - Verifies: exact request/response schemas, status codes, upload limits, camera probe, capability
    report, lifecycle conflicts, reset confirmation, and typed error envelope
  - Depends on: TASK-032

- [ ] **TASK-034** [M] Implement FastAPI shell and source/monitoring endpoints
  - Creates: `code/pick-zone-demo/app/api/app.py`,
    `code/pick-zone-demo/app/api/source_monitoring.py`
  - Contracts: `contracts/source-monitoring-api.md`
  - Plan: sections 4.13, 5, and 8
  - Satisfies: AC-1 through AC-4, AC-6, AC-7, AC-29, AC-30, AC-E1, AC-E8
  - Depends on: TASK-033

- [ ] **TASK-035** [M] Write profile/task/inventory HTTP API tests
  - Creates: `code/pick-zone-demo/tests/api/test_profiles_tasks.py`
  - Tests: AC-5, AC-6, AC-24, AC-28, AC-29
  - Verifies: exact schemas, source-profile CRUD, zone validation errors, task selection conflicts,
    and read-only inventory/SKU/location responses
  - Depends on: TASK-034

- [ ] **TASK-036** [M] Implement profile, task, and inventory endpoints
  - Creates: `code/pick-zone-demo/app/api/profiles_tasks.py`,
    `code/pick-zone-demo/app/services/profile_service.py`
  - Contracts: `contracts/profiles-tasks-api.md`, `contracts/workbook-contract.md`
  - Plan: sections 4.4 and 4.13
  - Satisfies: AC-5, AC-6, AC-24, AC-28, AC-29
  - Depends on: TASK-035

- [ ] **TASK-037** [M] Write event-review and evidence HTTP API tests
  - Creates: `code/pick-zone-demo/tests/api/test_events_review.py`
  - Tests: AC-19, AC-20, AC-21, AC-22, AC-23, AC-27, AC-29, AC-E7
  - Verifies: event list/detail, approve observed, edited approval validation, reject, evidence byte
    ranges, idempotency header, original/final result preservation, and every mutation error
  - Depends on: TASK-018, TASK-028, TASK-034

- [ ] **TASK-038** [M] Implement event-review and evidence endpoints
  - Creates: `code/pick-zone-demo/app/api/events_review.py`
  - Contracts: `contracts/events-review-api.md`, `contracts/workbook-contract.md`
  - Plan: sections 4.9, 4.11, and 4.13
  - Satisfies: AC-19 through AC-23, AC-27, AC-29, AC-E7
  - Depends on: TASK-037

- [ ] **TASK-039** [M] Write live WebSocket and MJPEG endpoint tests
  - Creates: `code/pick-zone-demo/tests/api/test_live_stream.py`
  - Tests: AC-8, AC-28, AC-E6
  - Verifies: full initial snapshot, ordered envelopes, latest-only slow-client behavior, reconnect,
    closure codes, preview availability, multipart MJPEG framing, and browser independence
  - Depends on: TASK-030, TASK-034

- [ ] **TASK-040** [M] Implement WebSocket state and MJPEG preview endpoints
  - Creates: `code/pick-zone-demo/app/api/live.py`
  - Contracts: `contracts/live-stream-contract.md`, `contracts/source-monitoring-api.md`
  - Plan: sections 4.12, 4.13, and 8
  - Satisfies: AC-8, AC-28, AC-E6
  - Depends on: TASK-039

### Browser Experience And Reporting

- [ ] **TASK-041** [M] Write browser dashboard shell and responsive-layout tests
  - Creates: `code/pick-zone-demo/tests/browser/test_dashboard.py`
  - Tests: AC-28, AC-29, AC-E6
  - Verifies: one-page monitoring surface, stable desktop/laptop layout, text status independent
    of color, required live fields, reconnect snapshot, and no overlapping controls
  - Depends on: TASK-036, TASK-038, TASK-040

- [ ] **TASK-042** [M] Implement the monitoring dashboard shell and live rendering
  - Creates: `code/pick-zone-demo/app/static/index.html`,
    `code/pick-zone-demo/app/static/styles.css`,
    `code/pick-zone-demo/app/static/app.js`
  - Contracts: `contracts/live-stream-contract.md`, `contracts/source-monitoring-api.md`
  - Plan: sections 4.13 and 8
  - Satisfies: AC-28, AC-29, AC-E6
  - Depends on: TASK-041

- [ ] **TASK-043** [M] Write browser calibration, controls, review, and evidence tests
  - Creates: `code/pick-zone-demo/tests/browser/test_workflows.py`
  - Tests: AC-5, AC-6, AC-21, AC-27, AC-29, AC-30, AC-E1
  - Verifies: five-or-fewer-action prepared start, zone drawing, disabled states, start/pause/resume/
    stop/reset, source errors, approve/edit/reject reason, original result, and evidence replay
  - Depends on: TASK-042

- [ ] **TASK-044** [M] Implement calibration, operator controls, and review workflows
  - Modifies: `code/pick-zone-demo/app/static/index.html`,
    `code/pick-zone-demo/app/static/styles.css`,
    `code/pick-zone-demo/app/static/app.js`
  - Contracts: `contracts/profiles-tasks-api.md`, `contracts/source-monitoring-api.md`,
    `contracts/events-review-api.md`
  - Plan: sections 4.13 and 8
  - Satisfies: AC-5, AC-6, AC-21, AC-27, AC-29, AC-30, AC-E1
  - Depends on: TASK-043

- [ ] **TASK-045** [S] Write session-metrics aggregation and export tests
  - Creates: `code/pick-zone-demo/tests/services/test_metrics.py`
  - Tests: AC-7, AC-8; AC-S2 and observability NFRs
  - Verifies: captured/analyzed/dropped counts, drop ratio, mean/p95 latency, queue depths, event
    and review totals, final outcomes, and stable CSV export
  - Depends on: TASK-030

- [ ] **TASK-046** [S] Implement session metrics and CSV export
  - Creates: `code/pick-zone-demo/app/services/metrics.py`
  - Contracts: `contracts/live-stream-contract.md`, `contracts/workbook-contract.md`
  - Plan: section 4.14
  - Satisfies: AC-7, AC-8; AC-S2 and observability NFRs
  - Depends on: TASK-045

### Demo Fixtures, Assembly, And Acceptance

- [ ] **TASK-047** [M] Write workbook/scenario fixture-factory tests
  - Creates: `code/pick-zone-demo/tests/fixtures/test_scenario_factory.py`
  - Tests: AC-24, AC-26, AC-30
  - Verifies: exact workbook headers, coherent SKU/location/task/inventory references, clean reset
    snapshot, deterministic replay sidecar, and no secret/network credentials
  - Depends on: TASK-016

- [ ] **TASK-048** [M] Implement reproducible demo workbook and replay-scenario generation
  - Creates: `code/pick-zone-demo/tools/create_demo_scenario.py`,
    `code/pick-zone-demo/app/persistence/fixtures.py`,
    `code/pick-zone-demo/demo/scenarios/pick-return.json`
  - Contracts: `contracts/workbook-contract.md`, `contracts/vision-domain-contract.md`
  - Plan: sections 7.3 and 9
  - Satisfies: AC-24, AC-26, AC-30
  - Depends on: TASK-047

- [ ] **TASK-049** [M] Write deterministic full-flow integration tests
  - Creates: `code/pick-zone-demo/tests/integration/test_replay_flow.py`
  - Tests: AC-1, AC-4 through AC-30, AC-E3 through AC-E8
  - Verifies: replay detections through tracking, pick/return, event completion, task match/mismatch,
    auto/manual decisions, evidence, atomic workbook reopen, browser disconnect, reset, and shutdown
  - Depends on: TASK-018, TASK-026, TASK-028, TASK-032, TASK-038, TASK-040, TASK-048

- [ ] **TASK-050** [M] Implement executable application composition and demo entry point
  - Creates: `code/pick-zone-demo/app/main.py`,
    `code/pick-zone-demo/app/services/demo_service.py`,
    `code/pick-zone-demo/run_demo.py`
  - Contracts: all files in `contracts/`
  - Plan: sections 3, 6, 8, 9, and implementation slices 1 through 5
  - Satisfies: AC-1, AC-4 through AC-30, AC-E3 through AC-E8
  - Depends on: TASK-049

- [ ] **TASK-051** [M] Add local-camera and synthetic-network integration tests
  - Creates: `code/pick-zone-demo/tests/integration/test_live_sources.py`,
    `code/pick-zone-demo/tests/fixtures/synthetic_stream.py`
  - Tests: AC-2, AC-3, AC-4, AC-E1, AC-E2, AC-E8
  - Verifies: simulated camera frames, unavailable camera, local HTTP stream, disconnect/reconnect,
    frozen interrupted event, responsive API during reconnect, and source release
  - Depends on: TASK-024, TASK-050

- [ ] **TASK-052** [M] Run end-to-end browser acceptance tests at laptop viewports
  - Modifies: `code/pick-zone-demo/tests/browser/test_dashboard.py`,
    `code/pick-zone-demo/tests/browser/test_workflows.py`
  - Tests: AC-5, AC-6, AC-21, AC-27 through AC-30, AC-E1, AC-E6
  - Verifies: screenshots at 1440x900 and 1280x720, nonblank preview, no text/control overlap,
    visible state/error feedback, full workflow, and current-state recovery after reconnect
  - Depends on: TASK-044, TASK-050

- [ ] **TASK-053** [M] Write staged-goods model qualification tests and thresholds
  - Creates: `code/pick-zone-demo/tests/acceptance/test_model_quality.py`,
    `code/pick-zone-demo/demo/model/validation-set.json`
  - Tests: AC-9, AC-10, AC-E3, AC-E4; AC-S1
  - Verifies: manifest/hash/license, supported staged classes, fixture precision/recall and crossing
    integrity thresholds, separable three-item cases, overlap review, and boundary-loss review
  - Depends on: TASK-026, TASK-050

- [ ] **TASK-054** [M] Prepare and qualify the licensed staged-goods ONNX artifact
  - Creates: `code/pick-zone-demo/demo/model/model.onnx`,
    `code/pick-zone-demo/demo/model/model-manifest.json`,
    `code/pick-zone-demo/demo/model/qualification-report.json`
  - Contracts: `contracts/vision-domain-contract.md`
  - Plan: sections 7.1 and 7.2; risk mitigations for detectability and licensing
  - Satisfies: AC-9, AC-10, AC-E3, AC-E4; AC-S1
  - Depends on: TASK-053

- [ ] **TASK-055** [M] Run the 30-minute throughput, latency, and shutdown acceptance benchmark
  - Creates: `code/pick-zone-demo/tests/acceptance/test_performance_soak.py`,
    `code/pick-zone-demo/demo/reports/performance-report.json`
  - Tests: AC-7, AC-8, AC-E5, AC-E8; performance, reliability, and observability NFRs
  - Verifies: 3/4/5 FPS within tolerance, p95 result age at most 1.0 second, bounded queues,
    resource metrics, no reversed actions, no worker failures, and shutdown within five seconds
  - Depends on: TASK-046, TASK-050, TASK-054

- [ ] **TASK-056** [S] Write operator runbook and acceptance-fixture provenance
  - Creates: `code/pick-zone-demo/README.md`,
    `code/pick-zone-demo/demo/README.md`
  - Documents: supported sources and staged goods, installation/run commands, workbook ownership,
    camera/RTSP setup, demo reset, review policy, model license/source, known limits, and test commands
  - Depends on: TASK-051, TASK-052, TASK-055

## Acceptance-Criteria Traceability

| Criteria | Primary test tasks |
|---|---|
| AC-1 through AC-4 | TASK-021, TASK-023, TASK-033, TASK-049, TASK-051 |
| AC-5 through AC-8 | TASK-003, TASK-019, TASK-031, TASK-033, TASK-035, TASK-049, TASK-055 |
| AC-9 through AC-13 | TASK-005, TASK-007, TASK-009, TASK-029, TASK-053 |
| AC-14 through AC-17 | TASK-011, TASK-029, TASK-049 |
| AC-18 through AC-23 | TASK-013, TASK-017, TASK-037, TASK-049 |
| AC-24 through AC-27 | TASK-015, TASK-017, TASK-027, TASK-037, TASK-047, TASK-049 |
| AC-28 through AC-30 | TASK-029, TASK-031, TASK-033, TASK-041, TASK-043, TASK-049, TASK-052 |
| AC-E1 through AC-E2 | TASK-021, TASK-023, TASK-033, TASK-043, TASK-051 |
| AC-E3 through AC-E5 | TASK-005, TASK-007, TASK-009, TASK-011, TASK-019, TASK-049, TASK-053, TASK-055 |
| AC-E6 through AC-E8 | TASK-011, TASK-017, TASK-019, TASK-027, TASK-031, TASK-049, TASK-052, TASK-055 |
| AC-S1, AC-S2 | TASK-005, TASK-007, TASK-025, TASK-045, TASK-053 |

## Dependency Milestones

1. **Deterministic core ready:** TASK-014
2. **Atomic workbook ready:** TASK-018
3. **All source adapters ready:** TASK-024
4. **Runtime orchestration ready:** TASK-032
5. **HTTP and live APIs ready:** TASK-040
6. **Operator dashboard ready:** TASK-044
7. **Deterministic executable demo ready:** TASK-050
8. **Live-source acceptance ready:** TASK-051
9. **Qualified live detector ready:** TASK-054
10. **Release candidate ready for Gate 5:** TASK-056

## Legend

- `[S]` Small: under 1 hour
- `[M]` Medium: 1-3 hours
- `[P]` Parallelizable with other ready `[P]` tasks that do not share modified files

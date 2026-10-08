"""Monitoring lifecycle: start/pause/resume/stop/reset/shutdown and the per-session workers.

Worker graph for one session::

    pick-zone-capture  -> capture buffer -> fan-out -> analysis / preview / evidence buffers
    pick-zone-analyzer -> FrameSampler -> OrderedAnalyzer (detector, tracker, crossing, events)
    pick-zone-preview  -> RuntimeStateStore.latest frame -> annotated JPEG + metrics (10 FPS)
    pick-zone-evidence -> EvidenceManager ring + clip finalization (only when FFmpeg exists)

Every buffer is a single-slot ``LatestFrameBuffer``; stale frames are overwritten and counted,
so no queue grows when inference is slow. Stop and shutdown finish within the timeout or
report ``SHUTDOWN_TIMEOUT``.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.core.context import ApplicationContext
from app.domain.event_machine import SourceHealth
from app.domain.zones import ZoneProfile
from app.persistence.schema import WorkbookFailure
from app.persistence.workbook import WorkbookSnapshot
from app.services.orchestrator import (
    BusinessEventService,
    MasterData,
    OrderedAnalyzer,
    build_audit_mutation,
    build_session_mutation,
    iso,
)
from app.services.runtime_state import Alert, InventoryView
from app.video.buffer import LatestFrameBuffer
from app.video.capture import CaptureWorker
from app.video.sampler import FrameSampler
from app.video.sources import ReadKind, SourceFailure, VideoSource
from app.vision.detector import Detector
from app.vision.overlay import PreviewRenderer

Row = Mapping[str, Any]
ALLOWED_ANALYSIS_FPS = frozenset({3, 4, 5})
OPEN_TASK_STATUSES = frozenset({"open", "in_progress"})
RECONNECTING_CODES = frozenset({"SOURCE_RECONNECTING", "SOURCE_DISCONNECTED"})
DEFAULT_SHUTDOWN_TIMEOUT_S = 5.0
METRICS_INTERVAL_S = 0.2

ERROR_STATUS = {
    "VALIDATION_ERROR": 400,
    "SOURCE_PROFILE_NOT_FOUND": 404,
    "ZONE_PROFILE_NOT_FOUND": 404,
    "TASK_NOT_FOUND": 404,
    "SCENARIO_NOT_FOUND": 404,
    "MONITORING_ALREADY_ACTIVE": 409,
    "PROFILE_TASK_MISMATCH": 409,
    "PERSISTENCE_BLOCKED": 409,
    "INVALID_MONITORING_STATE": 409,
    "ACTIVE_EVENT_REQUIRES_DECISION": 409,
    "MONITORING_ACTIVE": 409,
    "SOURCE_OPEN_FAILED": 422,
    "SOURCE_UNAVAILABLE": 422,
    "SHUTDOWN_TIMEOUT": 503,
}
# Missing-prerequisite kinds that have their own contract error code, in precedence order.
PREREQUISITE_CODES = (
    ("source_profile", "SOURCE_PROFILE_NOT_FOUND"),
    ("zone_profile", "ZONE_PROFILE_NOT_FOUND"),
    ("task", "TASK_NOT_FOUND"),
)


class MonitoringState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    FAILED = "failed"


class LifecycleError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        *,
        status: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        self.status = status if status is not None else ERROR_STATUS.get(code, 400)


@dataclass(frozen=True, slots=True)
class StartRequest:
    source_profile_id: str
    zone_profile_id: str
    task_id: str
    analysis_fps: int = 4
    network_url_override: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class StartResult:
    session_id: UUID
    state: str = MonitoringState.STARTING.value


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: UUID
    state: str
    captured_frames: int
    analyzed_frames: int
    dropped_frames: int
    late_results: int
    discarded_event_id: UUID | None


@dataclass(frozen=True, slots=True)
class ResetResult:
    scenario_id: str
    workbook_version: int
    inventory: tuple[InventoryView, ...]


class RateMeter:
    """Events per second over a sliding window."""

    def __init__(self, window_s: float = 2.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._window_s = window_s
        self._clock = clock
        self._ticks: deque[float] = deque()
        self._lock = threading.Lock()

    def tick(self) -> None:
        with self._lock:
            now = self._clock()
            self._ticks.append(now)
            self._trim(now)

    def rate(self) -> float:
        with self._lock:
            now = self._clock()
            self._trim(now)
            if len(self._ticks) < 2:
                return 0.0
            span = max(now - self._ticks[0], 1e-6)
            return round(len(self._ticks) / max(span, self._window_s / 4), 2)

    def _trim(self, now: float) -> None:
        while self._ticks and now - self._ticks[0] > self._window_s:
            self._ticks.popleft()


@dataclass(slots=True)
class _Plan:
    source_profile: Row
    zone: ZoneProfile
    master: MasterData
    detector: Detector
    allowed_class_names: frozenset[str]


@dataclass(slots=True)
class _Session:
    session_id: UUID
    request: StartRequest
    plan: _Plan
    source: VideoSource
    capture: CaptureWorker
    capture_buffer: LatestFrameBuffer
    consumers: tuple[LatestFrameBuffer, ...]
    analysis_buffer: LatestFrameBuffer
    preview_buffer: LatestFrameBuffer
    evidence_buffer: LatestFrameBuffer | None
    sampler: FrameSampler
    analyzer: OrderedAnalyzer
    business: BusinessEventService
    evidence: Any | None
    renderer: PreviewRenderer
    started_at: datetime
    stop_event: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list)
    captured: int = 0
    source_state: str = "connected"
    capture_rate: RateMeter = field(default_factory=RateMeter)
    analysis_rate: RateMeter = field(default_factory=RateMeter)
    preview_rate: RateMeter = field(default_factory=RateMeter)


class LifecycleService:
    def __init__(
        self,
        context: ApplicationContext,
        *,
        shutdown_timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S,
        preview_fps: int = 10,
        preview_width: int = 960,
        poll_s: float = 0.05,
    ) -> None:
        self._context = context
        self._shutdown_timeout_s = shutdown_timeout_s
        self._preview_interval_s = 1 / preview_fps
        self._preview_width = preview_width
        self._poll_s = poll_s
        self._lock = threading.RLock()
        self._state = MonitoringState.STOPPED
        self._session: _Session | None = None
        self._leftover_threads: list[threading.Thread] = []
        self._shut_down = False

    # ------------------------------------------------------------------ status

    @property
    def state(self) -> MonitoringState:
        return self._state

    @property
    def session_id(self) -> UUID | None:
        session = self._session
        return None if session is None else session.session_id

    def worker_names(self) -> set[str]:
        session = self._session
        if session is None:
            return set()
        return {thread.name for thread in session.threads if thread.is_alive()}

    def _set_state(self, state: MonitoringState) -> None:
        self._state = state
        self._context.runtime.update_monitoring(state=state.value)

    # ------------------------------------------------------------------ start

    def start(self, request: StartRequest) -> StartResult:
        with self._lock:
            if self._shut_down:
                raise LifecycleError("INVALID_MONITORING_STATE", "application is shutting down")
            if self._state not in {MonitoringState.STOPPED, MonitoringState.FAILED}:
                raise LifecycleError(
                    "MONITORING_ALREADY_ACTIVE", "stop the current session before starting"
                )
            if self._leftover_threads and any(t.is_alive() for t in self._leftover_threads):
                raise LifecycleError(
                    "MONITORING_ALREADY_ACTIVE", "previous session workers are still stopping"
                )
            if request.analysis_fps not in ALLOWED_ANALYSIS_FPS:
                raise LifecycleError(
                    "VALIDATION_ERROR",
                    "analysis_fps must be 3, 4, or 5",
                    {"field": "analysis_fps", "allowed": sorted(ALLOWED_ANALYSIS_FPS)},
                )
            context = self._context
            try:
                snapshot = context.load_snapshot()
            except WorkbookFailure as failure:
                context.persistence.mark_blocked(failure.code)
                raise LifecycleError(
                    "PERSISTENCE_BLOCKED", "workbook cannot be loaded", {"error": failure.code}
                ) from failure
            if not context.persistence.writable:
                raise LifecycleError(
                    "PERSISTENCE_BLOCKED",
                    "workbook cannot accept decisions",
                    {"persistence_state": context.persistence.state},
                )
            plan = self._plan(snapshot, request)
            return self._launch(request, plan)

    def _plan(self, snapshot: WorkbookSnapshot, request: StartRequest) -> _Plan:
        """Resolve every start prerequisite, reporting all missing ones together."""
        indexes = snapshot.indexes
        missing: list[dict[str, str]] = []

        def lack(kind: str, identifier: str, reason: str) -> None:
            missing.append({"kind": kind, "id": identifier, "reason": reason})

        profile = indexes.source_profile_by_id.get(request.source_profile_id)
        if profile is None:
            lack("source_profile", request.source_profile_id, "not_found")
        elif request.network_url_override and str(profile["source_type"]) != "network":
            raise LifecycleError(
                "VALIDATION_ERROR",
                "network_url_override is only accepted for a network source profile",
                {"field": "network_url_override"},
            )

        zone = next(
            (
                item
                for item in indexes.zone_by_source_version.values()
                if item.zone_profile_id == request.zone_profile_id
            ),
            None,
        )
        if zone is None or zone.source_profile_id != request.source_profile_id:
            lack("zone_profile", request.zone_profile_id, "not_found")
            zone = None
        elif not zone.active:
            lack("zone_profile", request.zone_profile_id, "inactive")

        task = indexes.task_by_id.get(request.task_id)
        sku: Row | None = None
        if task is None:
            lack("task", request.task_id, "not_found")
        else:
            if str(task["status"]) not in OPEN_TASK_STATUSES:
                lack("task", request.task_id, "not_open")
            sku_id = str(task["sku_id"])
            sku = indexes.sku_by_id.get(sku_id)
            if sku is None:
                lack("sku", sku_id, "not_found")
            elif not bool(sku["active"]):
                lack("sku", sku_id, "inactive")
            for kind, column in (
                ("source", "source_location_id"),
                ("destination", "destination_location_id"),
            ):
                location_id = str(task[column])
                location = indexes.location_by_id.get(location_id)
                if location is None:
                    lack(f"{kind}_location", location_id, "not_found")
                elif not bool(location["active"]):
                    lack(f"{kind}_location", location_id, "inactive")
                if (location_id, sku_id) not in indexes.inventory_by_location_sku:
                    lack(f"{kind}_inventory", f"{location_id}/{sku_id}", "not_found")

        detector: Detector | None = None
        allowed: frozenset[str] = frozenset()
        if not missing and profile is not None and sku is not None:
            allowed = frozenset({str(sku["detector_class"])})
            try:
                detector = self._context.detector_factory(profile, allowed)
            except Exception as error:
                lack("detector", str(profile["source_profile_id"]), _error_code(error))

        if missing or profile is None or zone is None or task is None or detector is None:
            kinds = {item["kind"] for item in missing}
            code = next(
                (code for kind, code in PREREQUISITE_CODES if kind in kinds), "VALIDATION_ERROR"
            )
            raise LifecycleError(
                code, "monitoring prerequisites are missing", {"missing": missing}
            )
        return _Plan(
            source_profile=profile,
            zone=zone,
            master=MasterData.from_snapshot(snapshot, request.task_id),
            detector=detector,
            allowed_class_names=allowed,
        )

    def _launch(self, request: StartRequest, plan: _Plan) -> StartResult:
        context = self._context
        runtime = context.runtime
        self._set_state(MonitoringState.STARTING)
        source: VideoSource | None = None
        try:
            source = context.source_factory(plan.source_profile, request.network_url_override)
            source.open()
        except Exception as error:
            if source is not None:
                with contextlib.suppress(Exception):
                    source.release()
            self._set_state(MonitoringState.STOPPED)
            raise LifecycleError(
                "SOURCE_OPEN_FAILED", "source cannot open", {"source_error": _error_code(error)}
            ) from error

        session_id = context.uuid_factory()
        zone = plan.zone
        master = plan.master
        runtime.reset_session(
            session_id,
            monitoring={
                "state": MonitoringState.STARTING.value,
                "source_profile_id": request.source_profile_id,
                "zone_profile_id": request.zone_profile_id,
                "task_id": request.task_id,
            },
            source={"state": "connected", "profile_id": request.source_profile_id},
        )
        runtime.update_task(master.task_view())
        runtime.update_inventory(master.inventory_view())
        runtime.update_persistence(
            state=context.persistence.state, last_commit_at=context.persistence.last_commit_at
        )

        evidence = context.evidence_factory(session_id)
        if evidence is None:
            runtime.raise_alert(
                Alert(
                    "EVIDENCE_UNAVAILABLE",
                    "warning",
                    "FFmpeg is unavailable; events are recorded without clips and need review.",
                )
            )
        business = BusinessEventService(
            context.persistence,
            runtime,
            master,
            session_id,
            reload=context.load_snapshot,
            now=context.now,
        )
        analyzer = OrderedAnalyzer(
            session_id=session_id,
            zone=zone,
            event_context=master.event_context(
                zone,
                session_id,
                f"{zone.zone_profile_id}@v{zone.version};fps={request.analysis_fps}",
            ),
            detector=plan.detector,
            runtime=runtime,
            business=business,
            allowed_class_names=plan.allowed_class_names,
            evidence=evidence,
            uuid_factory=context.uuid_factory,
            wall_now=context.now,
            monotonic_ns=context.monotonic_ns,
        )
        capture_buffer = LatestFrameBuffer()
        analysis_buffer = LatestFrameBuffer()
        preview_buffer = LatestFrameBuffer()
        evidence_buffer = LatestFrameBuffer() if evidence is not None else None
        consumers = tuple(
            item for item in (analysis_buffer, preview_buffer, evidence_buffer) if item is not None
        )
        session = _Session(
            session_id=session_id,
            request=request,
            plan=plan,
            source=source,
            capture=CaptureWorker(
                source, capture_buffer, session_id, monotonic_ns=context.monotonic_ns
            ),
            capture_buffer=capture_buffer,
            consumers=consumers,
            analysis_buffer=analysis_buffer,
            preview_buffer=preview_buffer,
            evidence_buffer=evidence_buffer,
            sampler=FrameSampler(request.analysis_fps),
            analyzer=analyzer,
            business=business,
            evidence=evidence,
            renderer=PreviewRenderer(
                runtime, zone_provider=lambda: zone, max_width=self._preview_width
            ),
            started_at=context.now(),
        )
        workers: list[tuple[str, Callable[[_Session], None]]] = [
            ("pick-zone-capture", self._capture_loop),
            ("pick-zone-analyzer", self._analysis_loop),
            ("pick-zone-preview", self._preview_loop),
        ]
        if evidence is not None:
            workers.append(("pick-zone-evidence", self._evidence_loop))
        for name, target in workers:
            thread = threading.Thread(target=target, args=(session,), name=name, daemon=True)
            session.threads.append(thread)
        self._session = session
        for thread in session.threads:
            thread.start()
        self._set_state(MonitoringState.RUNNING)
        self._persist_session(session, "running", action="session_started")
        return StartResult(session_id)

    # ------------------------------------------------------------------ workers

    def _capture_loop(self, session: _Session) -> None:
        runtime = self._context.runtime
        last_sequence = -1
        while not session.stop_event.is_set():
            try:
                result = session.capture.capture_once()
            except (SourceFailure, RuntimeError) as error:
                if not session.stop_event.is_set():
                    self._source_failed(session, _error_code(error))
                return
            if result.kind is ReadKind.FRAME:
                frame = session.capture_buffer.latest_after(last_sequence)
                if frame is None:
                    continue
                last_sequence = frame.sequence
                session.captured += 1
                session.capture_rate.tick()
                for buffer in session.consumers:
                    buffer.publish(frame)
                if session.source_state != "connected":
                    session.source_state = "connected"
                    session.analyzer.notify_source_health(SourceHealth.CONNECTED)
                    runtime.update_source(state="connected", error_code=None)
            elif result.kind is ReadKind.PAUSED:
                session.stop_event.wait(self._poll_s / 5)
            elif result.kind is ReadKind.ENDED:
                session.source_state = "ended"
                session.analyzer.notify_source_ended()
                runtime.update_source(state="ended")
                return
            else:
                code = result.error_code or "SOURCE_ERROR"
                if code in RECONNECTING_CODES:
                    if session.source_state != "reconnecting":
                        session.source_state = "reconnecting"
                        session.analyzer.notify_source_health(SourceHealth.RECONNECTING)
                    health = getattr(session.source, "health", None)
                    runtime.update_source(
                        state="reconnecting",
                        error_code=code,
                        reconnect_attempts=getattr(health, "reconnect_attempts", 0),
                        continuity_segment=session.source.continuity_segment,
                    )
                    session.stop_event.wait(self._poll_s / 5)
                    continue
                if code != "SOURCE_CLOSED":
                    self._source_failed(session, code)
                return

    def _source_failed(self, session: _Session, code: str) -> None:
        session.source_state = "failed"
        session.analyzer.notify_source_health(SourceHealth.FAILED)
        self._context.runtime.update_source(state="failed", error_code=code)
        self._context.runtime.raise_alert(
            Alert("SOURCE_FAILED", "error", f"Video source failed ({code}).")
        )

    def _analysis_loop(self, session: _Session) -> None:
        monotonic_ns = self._context.monotonic_ns
        last_sequence = -1
        while not session.stop_event.is_set():
            frame = session.analysis_buffer.wait_after(last_sequence, self._poll_s)
            if session.stop_event.is_set():
                return
            session.analyzer.process_pending()
            if frame is None:
                continue
            last_sequence = frame.sequence
            if session.analyzer.paused:
                continue
            decision = session.sampler.consider(frame, monotonic_ns())
            if not decision.selected:
                continue
            session.analyzer.analyze(frame)
            session.sampler.record_result(frame, monotonic_ns())
            session.analysis_rate.tick()

    def _preview_loop(self, session: _Session) -> None:
        runtime = self._context.runtime
        last_sequence = -1
        next_metrics = 0.0
        while not session.stop_event.is_set():
            started = time.monotonic()
            frame = session.preview_buffer.wait_after(last_sequence, self._preview_interval_s)
            if session.stop_event.is_set():
                return
            if frame is not None:
                last_sequence = frame.sequence
                runtime.publish_frame(frame)
                try:
                    if session.renderer.render_latest():
                        session.preview_rate.tick()
                except Exception as error:  # preview must never stop monitoring
                    runtime.raise_alert(
                        Alert("PREVIEW_UNAVAILABLE", "warning", f"Preview failed: {error}")
                    )
            if started >= next_metrics:
                self._publish_metrics(session)
                next_metrics = started + METRICS_INTERVAL_S
            remaining = self._preview_interval_s - (time.monotonic() - started)
            if remaining > 0:
                session.stop_event.wait(remaining)

    def _evidence_loop(self, session: _Session) -> None:
        assert session.evidence is not None and session.evidence_buffer is not None
        last_sequence = -1
        next_finalize = time.monotonic()
        while not session.stop_event.is_set():
            frame = session.evidence_buffer.wait_after(last_sequence, self._poll_s)
            if session.stop_event.is_set():
                return
            if frame is not None:
                last_sequence = frame.sequence
                session.evidence.add_frame(frame)
            if time.monotonic() >= next_finalize:
                # finalize_ready encodes ready clips synchronously on this thread
                for outcome in session.evidence.finalize_ready():
                    session.business.resolve_evidence(outcome)
                next_finalize = time.monotonic() + 0.25

    # ------------------------------------------------------------------ metrics and rows

    def _session_metrics(self, session: _Session) -> dict[str, Any]:
        analyzer = session.analyzer.metrics
        queue_depth = session.analysis_buffer.metrics.queue_depth
        return {
            "captured_frames": session.captured,
            "analyzed_frames": analyzer.analyzed_frames,
            "dropped_frames": max(0, session.captured - analyzer.analyzed_frames - queue_depth),
            "late_results": self._context.runtime.snapshot().metrics.late_results,
            "result_latency_ms": analyzer.mean_latency_ms,
            "result_latency_p95_ms": analyzer.p95_latency_ms,
            "analysis_queue_depth": queue_depth,
        }

    def _publish_metrics(self, session: _Session) -> None:
        metrics = self._session_metrics(session)
        self._context.runtime.update_metrics(
            capture_fps=session.capture_rate.rate(),
            analysis_fps=session.analysis_rate.rate(),
            preview_fps=session.preview_rate.rate(),
            captured_frames=metrics["captured_frames"],
            analyzed_frames=metrics["analyzed_frames"],
            dropped_frames=metrics["dropped_frames"],
            result_latency_ms=metrics["result_latency_ms"],
            result_latency_p95_ms=metrics["result_latency_p95_ms"],
            analysis_queue_depth=metrics["analysis_queue_depth"],
            persistence_queue_depth=self._context.persistence.queue_depth,
        )

    def _persist_session(
        self,
        session: _Session,
        status: str,
        *,
        action: str,
        error_code: str | None = None,
    ) -> bool:
        metrics = self._session_metrics(session)
        task = session.plan.master.task
        ended = status in {"stopped", "failed"}
        row = {
            "session_id": str(session.session_id),
            "source_profile_id": session.request.source_profile_id,
            "zone_profile_id": session.request.zone_profile_id,
            "task_id": task.task_id,
            "sku_id": task.sku_id,
            "location_id": task.source_location_id,
            "status": status,
            "started_at": iso(session.started_at),
            "ended_at": iso(self._context.now()) if ended else None,
            "captured_frames": metrics["captured_frames"],
            "analyzed_frames": metrics["analyzed_frames"],
            "dropped_frames": metrics["dropped_frames"],
            "late_results": metrics["late_results"],
            "mean_latency_ms": metrics["result_latency_ms"],
            "p95_latency_ms": metrics["result_latency_p95_ms"],
            "error_code": error_code,
        }
        timestamp = self._context.now()
        return self._context.persistence.submit(
            lambda version: build_session_mutation(
                version, session_row=row, action=action, timestamp=timestamp
            ),
            label=action,
        )

    # ------------------------------------------------------------------ pause / resume

    def pause(self) -> MonitoringState:
        with self._lock:
            session = self._session
            if self._state is not MonitoringState.RUNNING or session is None:
                raise LifecycleError("INVALID_MONITORING_STATE", "monitoring is not running")
            session.analyzer.pause()
            pause_source = getattr(session.source, "pause", None)
            if callable(pause_source):
                pause_source()  # file playback stops; live sources keep the latest frame only
            self._set_state(MonitoringState.PAUSED)
            return self._state

    def resume(self) -> MonitoringState:
        with self._lock:
            session = self._session
            if self._state is not MonitoringState.PAUSED or session is None:
                raise LifecycleError("INVALID_MONITORING_STATE", "monitoring is not paused")
            if session.source_state in {"ended", "failed"}:
                raise LifecycleError(
                    "SOURCE_UNAVAILABLE",
                    "the source is no longer available",
                    {"source_state": session.source_state},
                )
            resume_source = getattr(session.source, "resume", None)
            if callable(resume_source):
                resume_source()
            session.analyzer.resume()
            self._set_state(MonitoringState.RUNNING)
            return self._state

    # ------------------------------------------------------------------ stop

    def stop(
        self, discard_in_progress: bool = False, *, timeout_s: float | None = None
    ) -> SessionSummary:
        with self._lock:
            session = self._session
            if (
                self._state not in {MonitoringState.RUNNING, MonitoringState.PAUSED}
                or session is None
            ):
                raise LifecycleError("INVALID_MONITORING_STATE", "monitoring is not active")
            if not discard_in_progress and session.analyzer.has_open_event():
                raise LifecycleError(
                    "ACTIVE_EVENT_REQUIRES_DECISION",
                    "an event is in progress; stop with discard_in_progress to discard it",
                    {"event_id": str(session.analyzer.active_event().event_id)},
                )
            reason = "operator_discard" if discard_in_progress else "stopped_during_event"
            return self._stop_session(
                session, reason, timeout_s if timeout_s is not None else self._shutdown_timeout_s
            )

    def _stop_session(self, session: _Session, reason: str, timeout_s: float) -> SessionSummary:
        deadline = time.monotonic() + timeout_s

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        runtime = self._context.runtime
        self._set_state(MonitoringState.STOPPING)
        session.stop_event.set()
        session.sampler.cancel()
        for buffer in (*session.consumers, session.capture_buffer):
            buffer.close()
        capture_thread = session.threads[0]
        capture_thread.join(min(1.0, remaining()))
        session.capture.stop()  # releases the source
        runtime.update_source(state="closed")
        for thread in session.threads[1:]:
            thread.join(remaining())
        alive = [thread for thread in session.threads if thread.is_alive()]
        if alive:
            self._leftover_threads = alive
            self._set_state(MonitoringState.FAILED)
            self._persist_session(
                session, "failed", action="session_failed", error_code="SHUTDOWN_TIMEOUT"
            )
            self._session = None
            raise LifecycleError(
                "SHUTDOWN_TIMEOUT",
                "workers did not stop in time",
                {"threads": sorted(thread.name for thread in alive)},
            )

        discarded = session.analyzer.discard_open_event(reason)
        if discarded is not None:
            session.business.record_discard(discarded, reason)
        if session.evidence is not None:
            for outcome in session.evidence.shutdown(remaining()):
                session.business.resolve_evidence(outcome)
        session.business.resolve_all_without_evidence()
        self._publish_metrics(session)
        self._persist_session(session, "stopped", action="session_stopped")
        self._context.persistence.drain(remaining())
        metrics = self._session_metrics(session)
        self._session = None
        runtime.update_monitoring(state=MonitoringState.STOPPED.value, active_event_id=None)
        self._state = MonitoringState.STOPPED
        return SessionSummary(
            session_id=session.session_id,
            state=MonitoringState.STOPPED.value,
            captured_frames=metrics["captured_frames"],
            analyzed_frames=metrics["analyzed_frames"],
            dropped_frames=metrics["dropped_frames"],
            late_results=metrics["late_results"],
            discarded_event_id=None if discarded is None else discarded.event_id,
        )

    # ------------------------------------------------------------------ reset

    def reset(self, scenario_id: str, confirm: bool) -> ResetResult:
        with self._lock:
            if self._state is not MonitoringState.STOPPED:
                raise LifecycleError("MONITORING_ACTIVE", "stop monitoring before resetting")
            if not confirm:
                raise LifecycleError(
                    "VALIDATION_ERROR", "reset must be confirmed", {"field": "confirm"}
                )
            context = self._context
            restorer = context.scenario_restorer
            if restorer is None:
                raise LifecycleError(
                    "SCENARIO_NOT_FOUND",
                    "no scenarios are configured",
                    {"scenario_id": scenario_id},
                )
            try:
                restorer(scenario_id)
            except KeyError as error:
                raise LifecycleError(
                    "SCENARIO_NOT_FOUND", "unknown scenario", {"scenario_id": scenario_id}
                ) from error
            except (WorkbookFailure, OSError) as error:
                raise LifecycleError(
                    "PERSISTENCE_BLOCKED",
                    "scenario could not be restored",
                    {"error": _error_code(error)},
                    status=503,
                ) from error
            snapshot = context.refresh_persistence()
            if snapshot is None:
                raise LifecycleError(
                    "PERSISTENCE_BLOCKED",
                    "restored workbook cannot be loaded",
                    {"persistence_state": context.persistence.state},
                    status=503,
                )
            timestamp = context.now()
            key = f"scenario:{scenario_id}:reset:{context.uuid_factory()}"
            context.persistence.submit(
                lambda version: build_audit_mutation(
                    version,
                    idempotency_key=key,
                    action="scenario_reset",
                    entity_type="scenario",
                    entity_id=scenario_id,
                    timestamp=timestamp,
                    details={"workbook_version_before_audit": snapshot.workbook_version},
                ),
                label="scenario_reset",
            )
            context.persistence.drain(self._shutdown_timeout_s)
            context.runtime.reset_session(None)
            context.runtime.update_task(None)
            context.runtime.update_inventory(None)
            inventory = tuple(
                InventoryView(
                    str(row["location_id"]),
                    str(row["sku_id"]),
                    int(row["quantity"]),
                    int(row["version"]),
                )
                for row in sorted(
                    snapshot.rows["Inventory"],
                    key=lambda item: (str(item["location_id"]), str(item["sku_id"])),
                )
            )
            return ResetResult(scenario_id, context.persistence.workbook_version, inventory)

    # ------------------------------------------------------------------ shutdown

    def shutdown(self, timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S) -> None:
        """Stop monitoring (discarding any open event), flush persistence, stop the writer."""
        with self._lock:
            if self._shut_down:
                return
            deadline = time.monotonic() + timeout_s
            error: LifecycleError | None = None
            session = self._session
            if session is not None and self._state in {
                MonitoringState.RUNNING,
                MonitoringState.PAUSED,
            }:
                try:
                    self._stop_session(session, "shutdown", timeout_s)
                except LifecycleError as failure:
                    error = failure
            for thread in self._leftover_threads:
                thread.join(max(0.0, deadline - time.monotonic()))
            still_alive = [thread.name for thread in self._leftover_threads if thread.is_alive()]
            self._shut_down = True
            closed = self._context.close(max(0.0, deadline - time.monotonic()))
            if error is None and still_alive:
                error = LifecycleError(
                    "SHUTDOWN_TIMEOUT", "workers did not stop in time", {"threads": still_alive}
                )
            if error is None and not closed:
                error = LifecycleError("SHUTDOWN_TIMEOUT", "workbook writer did not stop in time")
            if error is None:
                self._leftover_threads = []
                self._state = MonitoringState.STOPPED
                self._context.runtime.update_monitoring(state=MonitoringState.STOPPED.value)
            else:
                self._set_state(MonitoringState.FAILED)
                raise error


def _error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    return str(code) if code else type(error).__name__

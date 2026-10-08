"""Ordered per-session analysis, business decisions, and serialized workbook persistence.

``OrderedAnalyzer`` runs detector -> tracker -> crossing -> event machine strictly in frame
order for one session. Terminal events go to ``BusinessEventService``, which waits for the
evidence clip when an evidence manager exists, reconciles the event against the selected task,
and submits the resulting mutation through ``PersistenceChannel``. The channel keeps one
mutation in flight so every mutation is built against the latest committed workbook version.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.domain.crossing import CrossingEngine
from app.domain.event_machine import (
    EventContext,
    EventFrame,
    EventMachine,
    EventSnapshot,
    EventState,
    SourceHealth,
)
from app.domain.mutations import (
    InventoryBalance,
    MutationPrecondition,
    PickTask,
    TaskStatus,
    WorkbookMutation,
    canonical_hash,
    stable_uuid,
)
from app.domain.zones import BoundarySide, ZoneProfile
from app.persistence.schema import WorkbookFailure
from app.persistence.workbook import WorkbookSnapshot
from app.persistence.writer import CommitReceipt
from app.services.inventory_service import InventoryService
from app.services.reconciliation import Decision, ReconciliationResult, ReconciliationService
from app.services.runtime_state import (
    ActionView,
    Alert,
    AnalysisView,
    DetectionView,
    EventView,
    InventoryView,
    RuntimeStateStore,
    TaskView,
    TrackView,
)
from app.video.evidence import EVIDENCE_UNAVAILABLE, EvidenceOutcome
from app.vision.detector import DetectionContext, DetectionFailure, Detector, FrameEnvelope
from app.vision.tracker import LightweightTracker, TrackObservation, TrackState

Row = Mapping[str, Any]

PERSISTENCE_CAPACITY = 100
# Failures that mean the workbook cannot currently be written at all.
BLOCKING_FAILURE_CODES = frozenset(
    {
        "WORKBOOK_NOT_FOUND",
        "WORKBOOK_LOCKED",
        "WORKBOOK_CORRUPT",
        "TEMP_SAVE_FAILED",
        "ATOMIC_REPLACE_FAILED",
        "POST_WRITE_VERIFY_FAILED",
        "PERSISTENCE_BLOCKED",
        "PATH_NOT_ALLOWED",
        "SHEET_MISSING",
        "HEADER_MISMATCH",
        "SCHEMA_VERSION_UNSUPPORTED",
    }
)
# A live track slower than this fraction of the frame width per second counts as still.
STABLE_SPEED_NORM_PER_SECOND = 0.02
TERMINAL_EVENT_STATES = frozenset(
    {EventState.COMPLETED, EventState.REVIEW_REQUIRED, EventState.APPROVED, EventState.REJECTED}
)
OPEN_EVENT_STATES = frozenset({EventState.ACTIVE, EventState.SETTLING})
LIVE_TRACK_STATES = frozenset({TrackState.TENTATIVE, TrackState.CONFIRMED})
OPEN_TASK_STATUSES = frozenset({TaskStatus.OPEN, TaskStatus.IN_PROGRESS})


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


# --------------------------------------------------------------------------- persistence


class PersistenceState(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PersistenceOutcome:
    label: str
    receipt: CommitReceipt | None
    error_code: str | None

    @property
    def ok(self) -> bool:
        return self.receipt is not None


MutationBuilder = Callable[[int], WorkbookMutation]


class MutationSink(Protocol):
    def submit(self, mutation: WorkbookMutation) -> Future[CommitReceipt]: ...


@dataclass(slots=True)
class _Job:
    builder: MutationBuilder
    label: str
    on_done: Callable[[PersistenceOutcome], None] | None


class PersistenceChannel:
    """Bounded, serial front of the workbook writer worker.

    Only one mutation is handed to the writer at a time, so each builder receives the workbook
    version committed by its predecessor. Any write failure disables further submissions
    (``writable`` becomes false) until ``recover`` is called; a rejected mutation that leaves the
    workbook readable (for example ``STALE_SNAPSHOT``) reloads the version and stays ready.
    """

    def __init__(
        self,
        sink: MutationSink,
        *,
        workbook_version: int = 0,
        capacity: int = PERSISTENCE_CAPACITY,
        reload_version: Callable[[], int] | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if capacity <= 0:
            raise ValueError("persistence capacity must be positive")
        self._sink = sink
        self._capacity = capacity
        self._reload_version = reload_version
        self._now = now
        self._condition = threading.Condition()
        self._backlog: deque[_Job] = deque()
        self._in_flight: _Job | None = None
        self._version = workbook_version
        self._state = PersistenceState.READY
        self._error_code: str | None = None
        self._last_commit_at: datetime | None = None
        self._closed = False
        self._listeners: list[Callable[[PersistenceChannel], None]] = []

    # ------------------------------------------------------------------ status

    @property
    def state(self) -> str:
        return self._state.value

    @property
    def error_code(self) -> str | None:
        return self._error_code

    @property
    def workbook_version(self) -> int:
        with self._condition:
            return self._version

    @property
    def last_commit_at(self) -> datetime | None:
        return self._last_commit_at

    @property
    def queue_depth(self) -> int:
        with self._condition:
            return len(self._backlog) + int(self._in_flight is not None)

    @property
    def writable(self) -> bool:
        with self._condition:
            return (
                not self._closed
                and self._state is PersistenceState.READY
                and len(self._backlog) + int(self._in_flight is not None) < self._capacity
            )

    def add_listener(self, listener: Callable[[PersistenceChannel], None]) -> None:
        self._listeners.append(listener)

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener(self)

    # ------------------------------------------------------------------ control

    def recover(self, workbook_version: int) -> None:
        """Mark the workbook ready again at ``workbook_version`` (after a successful load)."""
        with self._condition:
            self._version = workbook_version
            self._state = PersistenceState.READY
            self._error_code = None
            self._closed = False
        self._notify()

    def mark_blocked(self, code: str) -> None:
        with self._condition:
            self._state = PersistenceState.BLOCKED
            self._error_code = code
        self._notify()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def submit(
        self,
        builder: MutationBuilder,
        *,
        label: str = "",
        on_done: Callable[[PersistenceOutcome], None] | None = None,
    ) -> bool:
        """Queue a mutation builder; returns ``False`` when persistence cannot accept it."""
        with self._condition:
            depth = len(self._backlog) + int(self._in_flight is not None)
            if self._closed or self._state is not PersistenceState.READY:
                return False
            if depth >= self._capacity:
                return False
            self._backlog.append(_Job(builder, label, on_done))
        self._pump()
        return True

    def drain(self, timeout_s: float | None = None) -> bool:
        """Wait until every queued mutation finished; ``False`` on timeout."""
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._backlog and self._in_flight is None, timeout_s
            )

    # ------------------------------------------------------------------ internals

    def _pump(self) -> None:
        while True:
            with self._condition:
                if self._in_flight is not None or not self._backlog:
                    return
                job = self._backlog.popleft()
                self._in_flight = job
                version = self._version
                ready = self._state is PersistenceState.READY
            if not ready:
                self._finish(job, None, self._error_code or "PERSISTENCE_BLOCKED", state=None)
                continue
            try:
                mutation = job.builder(version)
            except Exception as error:
                self._finish(job, None, f"BUILD_FAILED:{type(error).__name__}", state=None)
                continue
            try:
                future = self._sink.submit(mutation)
            except WorkbookFailure as error:
                self._finish(job, None, error.code, state=PersistenceState.BLOCKED)
                continue
            except RuntimeError:
                self._finish(job, None, "PERSISTENCE_BLOCKED", state=PersistenceState.BLOCKED)
                continue
            future.add_done_callback(lambda done, job=job: self._on_future(job, done))
            return

    def _on_future(self, job: _Job, future: Future[CommitReceipt]) -> None:
        try:
            receipt = future.result()
        except WorkbookFailure as error:
            if error.code in BLOCKING_FAILURE_CODES:
                self._finish(job, None, error.code, state=PersistenceState.BLOCKED)
            else:
                self._finish(job, None, error.code, state=self._reloaded_state())
        except BaseException as error:
            self._finish(job, None, type(error).__name__, state=PersistenceState.FAILED)
        else:
            self._finish(job, receipt, None, state=PersistenceState.READY)
        self._pump()

    def _reloaded_state(self) -> PersistenceState:
        """After a rejected mutation, stay ready only if the workbook still loads."""
        if self._reload_version is None:
            return PersistenceState.READY
        try:
            version = self._reload_version()
        except Exception:
            return PersistenceState.BLOCKED
        with self._condition:
            self._version = version
        return PersistenceState.READY

    def _finish(
        self,
        job: _Job,
        receipt: CommitReceipt | None,
        error_code: str | None,
        *,
        state: PersistenceState | None,
    ) -> None:
        changed = False
        with self._condition:
            if receipt is not None:
                self._version = max(self._version, receipt.workbook_version_after)
                self._last_commit_at = self._now()
                changed = True
            if state is not None and state is not self._state:
                self._state = state
                changed = True
            if error_code is not None and state is not None:
                self._error_code = error_code
        if job.on_done is not None:
            job.on_done(PersistenceOutcome(job.label, receipt, error_code))
        with self._condition:
            self._in_flight = None
            self._condition.notify_all()
        if changed:
            self._notify()


# --------------------------------------------------------------------------- mutation builders


def _mutation(
    version: int,
    idempotency_key: str,
    *,
    upserts: dict[str, tuple[dict[str, object], ...]] | None = None,
    appends: dict[str, tuple[dict[str, object], ...]] | None = None,
    preconditions: tuple[MutationPrecondition, ...] = (),
    request_id: UUID | None = None,
) -> WorkbookMutation:
    upserts = upserts or {}
    appends = appends or {}
    return WorkbookMutation(
        mutation_id=stable_uuid(idempotency_key, "mutation"),
        idempotency_key=idempotency_key,
        request_id=request_id,
        expected_workbook_version=version,
        preconditions=preconditions,
        upserts=upserts,
        appends=appends,
        deletes={},
        canonical_effects_hash=canonical_hash({"upserts": upserts, "appends": appends}),
    )


def audit_row(
    idempotency_key: str,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    timestamp: datetime,
    details: Mapping[str, object] | None = None,
    operator_id: str = "system",
    request_id: UUID | None = None,
    result: str = "success",
    error_code: str | None = None,
) -> dict[str, object]:
    return {
        "audit_id": str(stable_uuid(idempotency_key, "audit")),
        "timestamp": iso(timestamp),
        "operator_id": operator_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "request_id": None if request_id is None else str(request_id),
        "before_json": None,
        "after_json": None,
        "result": result,
        "error_code": error_code,
        "details_json": json.dumps(dict(details or {}), sort_keys=True, default=str),
    }


def build_audit_mutation(
    version: int,
    *,
    idempotency_key: str,
    action: str,
    entity_type: str,
    entity_id: str,
    timestamp: datetime,
    details: Mapping[str, object] | None = None,
    operator_id: str = "system",
    request_id: UUID | None = None,
) -> WorkbookMutation:
    """A mutation that only appends one AuditLog row."""
    row = audit_row(
        idempotency_key,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        timestamp=timestamp,
        details=details,
        operator_id=operator_id,
        request_id=request_id,
    )
    return _mutation(version, idempotency_key, appends={"AuditLog": (row,)}, request_id=request_id)


def build_session_mutation(
    version: int,
    *,
    session_row: Mapping[str, object],
    action: str,
    timestamp: datetime,
) -> WorkbookMutation:
    """Upsert one Sessions row and audit the lifecycle transition."""
    session_id = str(session_row["session_id"])
    key = f"session:{session_id}:{action}"
    audit = audit_row(
        key,
        action=action,
        entity_type="session",
        entity_id=session_id,
        timestamp=timestamp,
        details={"status": session_row.get("status")},
    )
    return _mutation(
        version, key, upserts={"Sessions": (dict(session_row),)}, appends={"AuditLog": (audit,)}
    )


@dataclass(frozen=True, slots=True)
class EventTiming:
    started_at: datetime | None = None
    ended_at: datetime | None = None
    last_action_at: datetime | None = None


def build_review_mutation(
    version: int,
    *,
    event: EventSnapshot,
    session_id: UUID,
    review_reason: str,
    evidence_path: str | None,
    timing: EventTiming,
    created_at: datetime,
) -> WorkbookMutation:
    """Persist an event awaiting human review: Events, EventActions, and AuditLog rows only.

    No inventory or task row is touched.
    """
    if event.event_id is None or event.context is None:
        raise ValueError("event identity and context are required")
    context = event.context
    event_id = str(event.event_id)
    key = f"event:{event_id}:review_required"
    timestamp = iso(created_at)
    event_row: dict[str, object] = {
        "event_id": event_id,
        "session_id": str(session_id),
        "task_id": context.task_id,
        "zone_profile_id": context.zone_profile_id,
        "zone_version": context.zone_version,
        "state": EventState.REVIEW_REQUIRED.value,
        "observed_sku_id": context.sku_id,
        "observed_quantity": event.net_quantity,
        "observed_unit": context.unit,
        "observed_source_location_id": context.source_location_id,
        "final_sku_id": None,
        "final_quantity": None,
        "final_unit": None,
        "final_source_location_id": None,
        "aggregate_confidence": event.aggregate_confidence,
        "integrity_flags_json": json.dumps(sorted(event.integrity_flags)),
        "review_reason": review_reason,
        "decision": "pending",
        "started_at": iso(timing.started_at),
        "ended_at": iso(timing.ended_at),
        "evidence_path": evidence_path,
        "created_at": timestamp,
        "decided_at": None,
        "applied_at": None,
        "mutation_id": None,
    }
    actions = tuple(
        {
            "action_id": str(item.action_id),
            "event_id": event_id,
            "action_sequence": item.action_sequence,
            "track_id": item.track_id,
            "transition_index": item.transition_index,
            "action_type": item.action_type.value,
            "delta": item.delta,
            "source_timestamp_ms": item.source_timestamp_ms,
            "frame_sequence": item.frame_sequence,
            "confidence": item.confidence,
            "bbox_json": json.dumps([float(value) for value in item.bbox_xyxy]),
            "created_at": timestamp,
        }
        for item in event.actions
    )
    audit = audit_row(
        key,
        action="review_required",
        entity_type="event",
        entity_id=event_id,
        timestamp=created_at,
        details={"review_reason": review_reason, "net_quantity": event.net_quantity},
    )
    return _mutation(
        version,
        key,
        upserts={"Events": (event_row,)},
        appends={"EventActions": actions, "AuditLog": (audit,)},
        preconditions=(MutationPrecondition("event", event_id, {"mutation_id": None}),),
    )


# --------------------------------------------------------------------------- master data


@dataclass(frozen=True, slots=True)
class MasterData:
    """The task and inventory balances an event is reconciled against."""

    task: PickTask
    source_inventory: InventoryBalance
    destination_inventory: InventoryBalance

    @staticmethod
    def _balance(row: Row) -> InventoryBalance:
        return InventoryBalance(
            inventory_id=str(row["inventory_id"]),
            location_id=str(row["location_id"]),
            sku_id=str(row["sku_id"]),
            quantity=int(row["quantity"]),
            unit=str(row["unit"]),
            version=int(row["version"]),
        )

    @classmethod
    def from_snapshot(cls, snapshot: WorkbookSnapshot, task_id: str) -> MasterData:
        """Build from a workbook snapshot; raises ``KeyError`` for missing rows."""
        indexes = snapshot.indexes
        row = indexes.task_by_id[task_id]
        task = PickTask(
            task_id=str(row["task_id"]),
            sku_id=str(row["sku_id"]),
            expected_quantity=int(row["expected_quantity"]),
            unit=str(row["unit"]),
            source_location_id=str(row["source_location_id"]),
            destination_location_id=str(row["destination_location_id"]),
            status=TaskStatus(str(row["status"])),
        )
        inventory = indexes.inventory_by_location_sku
        return cls(
            task=task,
            source_inventory=cls._balance(inventory[(task.source_location_id, task.sku_id)]),
            destination_inventory=cls._balance(
                inventory[(task.destination_location_id, task.sku_id)]
            ),
        )

    def event_context(
        self, zone: ZoneProfile, session_id: UUID, configuration_version: str
    ) -> EventContext:
        return EventContext(
            task_id=self.task.task_id,
            zone_profile_id=zone.zone_profile_id,
            zone_version=zone.version,
            sku_id=self.task.sku_id,
            unit=self.task.unit,
            source_location_id=self.task.source_location_id,
            destination_location_id=self.task.destination_location_id,
            source_session_id=session_id,
            configuration_version=configuration_version,
        )

    def task_view(self) -> TaskView:
        task = self.task
        return TaskView(
            task.task_id,
            task.sku_id,
            task.expected_quantity,
            task.unit,
            task.source_location_id,
            task.status.value,
        )

    def inventory_view(self) -> InventoryView:
        balance = self.source_inventory
        return InventoryView(balance.location_id, balance.sku_id, balance.quantity, balance.version)


def event_view(
    snapshot: EventSnapshot,
    *,
    state: str | None = None,
    review_reason: str | None = None,
    last_action_at: datetime | None = None,
) -> EventView:
    return EventView(
        event_id=snapshot.event_id,
        state=state or snapshot.state.value,
        pick_count=snapshot.pick_count,
        return_count=snapshot.return_count,
        net_quantity=snapshot.net_quantity,
        last_action_at=last_action_at,
        review_reason=review_reason if review_reason is not None else snapshot.review_reason,
    )


# --------------------------------------------------------------------------- business events


@dataclass(frozen=True, slots=True)
class EventDecisionRecord:
    event_id: UUID
    decision: str
    review_reason: str | None
    persisted: bool
    error_code: str | None = None


class BusinessEventService:
    """Turns terminal events into decisions and workbook mutations.

    Auto-approval is only attempted while persistence is writable; otherwise the event stays
    ``review_required`` in memory (``unpersisted_events``) with a visible alert.
    """

    def __init__(
        self,
        channel: PersistenceChannel,
        runtime: RuntimeStateStore,
        master: MasterData,
        session_id: UUID,
        *,
        reload: Callable[[], WorkbookSnapshot] | None = None,
        reconciliation: ReconciliationService | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._channel = channel
        self._runtime = runtime
        self._master = master
        self._session_id = session_id
        self._reload = reload
        self._now = now
        self._reconciliation = reconciliation or ReconciliationService(InventoryService(), now=now)
        self._lock = threading.RLock()
        self._awaiting: dict[UUID, tuple[EventSnapshot, EventTiming]] = {}
        self.decisions: list[EventDecisionRecord] = []
        self.unpersisted_events: dict[UUID, EventSnapshot] = {}

    @property
    def master(self) -> MasterData:
        with self._lock:
            return self._master

    @property
    def awaiting_evidence(self) -> int:
        with self._lock:
            return len(self._awaiting)

    def event_finished(
        self, snapshot: EventSnapshot, timing: EventTiming, *, await_evidence: bool
    ) -> None:
        if snapshot.event_id is None:
            return
        if await_evidence:
            with self._lock:
                self._awaiting[snapshot.event_id] = (snapshot, timing)
            return
        self._decide(snapshot, timing, None, frozenset({EVIDENCE_UNAVAILABLE}))

    def resolve_evidence(self, outcome: EvidenceOutcome) -> bool:
        with self._lock:
            pending = self._awaiting.pop(outcome.event_id, None)
        if pending is None:
            return False
        snapshot, timing = pending
        self._decide(snapshot, timing, outcome.evidence_path, outcome.integrity_flags)
        return True

    def resolve_all_without_evidence(self) -> int:
        """Decide every event still waiting for a clip as evidence-unavailable."""
        with self._lock:
            pending = list(self._awaiting.values())
            self._awaiting.clear()
        for snapshot, timing in pending:
            self._decide(snapshot, timing, None, frozenset({EVIDENCE_UNAVAILABLE}))
        return len(pending)

    def record_discard(self, snapshot: EventSnapshot, reason: str) -> bool:
        """Audit an incomplete event discarded on stop; no inventory or event row changes."""
        if snapshot.event_id is None:
            return False
        event_id = str(snapshot.event_id)
        timestamp = self._now()
        details = {
            "session_id": str(self._session_id),
            "reason": reason,
            "state": snapshot.state.value,
            "pick_count": snapshot.pick_count,
            "return_count": snapshot.return_count,
            "net_quantity": snapshot.net_quantity,
        }
        return self._channel.submit(
            lambda version: build_audit_mutation(
                version,
                idempotency_key=f"event:{event_id}:discarded",
                action="event_discarded",
                entity_type="event",
                entity_id=event_id,
                timestamp=timestamp,
                details=details,
            ),
            label="discard",
        )

    # ------------------------------------------------------------------ decisions

    def _decide(
        self,
        snapshot: EventSnapshot,
        timing: EventTiming,
        evidence_path: str | None,
        flags: frozenset[str],
    ) -> None:
        snapshot = dataclasses.replace(snapshot, integrity_flags=snapshot.integrity_flags | flags)
        master = self.master
        try:
            result = self._reconciliation.reconcile(
                snapshot,
                master.task,
                master.source_inventory,
                master.destination_inventory,
                evidence_path=evidence_path,
                persistence_writable=self._channel.writable,
            )
        except ValueError as error:
            result = ReconciliationResult(Decision.REVIEW_REQUIRED, str(error), (), None)
        if result.mutation is None:
            self._persist_review(
                snapshot, timing, result.review_reason or "REVIEW_REQUIRED", evidence_path
            )
            return
        mutation = result.mutation
        accepted = self._channel.submit(
            lambda version: dataclasses.replace(mutation, expected_workbook_version=version),
            label=result.decision.value,
            on_done=lambda outcome: self._decision_done(
                snapshot, timing, result, evidence_path, outcome
            ),
        )
        if not accepted:
            self._persist_review(snapshot, timing, "PERSISTENCE_BLOCKED", evidence_path)

    def _decision_done(
        self,
        snapshot: EventSnapshot,
        timing: EventTiming,
        result: ReconciliationResult,
        evidence_path: str | None,
        outcome: PersistenceOutcome,
    ) -> None:
        assert snapshot.event_id is not None
        if not outcome.ok:
            self._runtime.raise_alert(
                Alert(
                    "DECISION_NOT_SAVED",
                    "error",
                    f"Workbook rejected the {result.decision.value} decision for event "
                    f"{snapshot.event_id} ({outcome.error_code}); it needs review.",
                )
            )
            self._persist_review(
                snapshot, timing, outcome.error_code or "PERSISTENCE_BLOCKED", evidence_path
            )
            return
        with self._lock:
            self.decisions.append(
                EventDecisionRecord(snapshot.event_id, result.decision.value, None, True)
            )
        self._runtime.update_event(
            event_view(
                snapshot, state=EventState.APPROVED.value, last_action_at=timing.last_action_at
            ),
            replace_only=snapshot.event_id,
        )
        self._refresh_master()

    def _persist_review(
        self,
        snapshot: EventSnapshot,
        timing: EventTiming,
        reason: str,
        evidence_path: str | None,
    ) -> None:
        assert snapshot.event_id is not None
        event_id = snapshot.event_id
        review = dataclasses.replace(
            snapshot, state=EventState.REVIEW_REQUIRED, review_reason=reason
        )
        self._runtime.update_event(
            event_view(review, last_action_at=timing.last_action_at), replace_only=event_id
        )
        created_at = self._now()

        def done(outcome: PersistenceOutcome) -> None:
            if outcome.ok:
                self._record(review, reason, persisted=True)
            else:
                self._keep_in_memory(review, reason, outcome.error_code)

        accepted = self._channel.writable and self._channel.submit(
            lambda version: build_review_mutation(
                version,
                event=review,
                session_id=self._session_id,
                review_reason=reason,
                evidence_path=evidence_path,
                timing=timing,
                created_at=created_at,
            ),
            label="review_required",
            on_done=done,
        )
        if not accepted:
            self._keep_in_memory(review, reason, self._channel.error_code or "PERSISTENCE_BLOCKED")

    def _record(self, snapshot: EventSnapshot, reason: str, *, persisted: bool) -> None:
        assert snapshot.event_id is not None
        with self._lock:
            self.decisions.append(
                EventDecisionRecord(
                    snapshot.event_id, Decision.REVIEW_REQUIRED.value, reason, persisted
                )
            )

    def _keep_in_memory(self, snapshot: EventSnapshot, reason: str, code: str | None) -> None:
        assert snapshot.event_id is not None
        with self._lock:
            self.unpersisted_events[snapshot.event_id] = snapshot
            self.decisions.append(
                EventDecisionRecord(
                    snapshot.event_id, Decision.REVIEW_REQUIRED.value, reason, False, code
                )
            )
        self._runtime.raise_alert(
            Alert(
                "PERSISTENCE_BLOCKED",
                "error",
                "Workbook unavailable; events stay in review in memory and auto-approval is "
                "disabled.",
            )
        )

    def _refresh_master(self) -> None:
        if self._reload is None:
            return
        try:
            master = MasterData.from_snapshot(self._reload(), self._master.task.task_id)
        except Exception:
            return
        with self._lock:
            self._master = master
        self._runtime.update_task(master.task_view())
        self._runtime.update_inventory(master.inventory_view())


# --------------------------------------------------------------------------- ordered analyzer


class EvidenceSink(Protocol):
    def begin_event(self, event_id: UUID, first_action_source_ms: int) -> None: ...

    def complete_event(self, event_id: UUID, ended_source_ms: int) -> None: ...

    def abandon_event(self, event_id: UUID, reason: str) -> EvidenceOutcome: ...

    def mark_source_ended(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AnalyzerMetrics:
    analyzed_frames: int
    late_results: int
    detection_failures: int
    mean_latency_ms: float | None
    p95_latency_ms: float | None


@dataclass(slots=True)
class _Pending:
    health: SourceHealth | None = None
    source_ended: bool = False
    paused: bool = False
    resumed: bool = False
    timing: EventTiming = field(default_factory=EventTiming)


class OrderedAnalyzer:
    """Strictly ordered detector -> tracker -> crossing -> event machine for one session."""

    def __init__(
        self,
        *,
        session_id: UUID,
        zone: ZoneProfile,
        event_context: EventContext,
        detector: Detector,
        runtime: RuntimeStateStore,
        business: BusinessEventService,
        allowed_class_names: frozenset[str],
        detection_threshold: float = 0.5,
        evidence: EvidenceSink | None = None,
        tracker: LightweightTracker | None = None,
        crossing: CrossingEngine | None = None,
        machine: EventMachine | None = None,
        uuid_factory: Callable[[], UUID] = uuid4,
        wall_now: Callable[[], datetime] = utc_now,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._session_id = session_id
        self._zone = zone
        self._event_context = event_context
        self._detector = detector
        self._runtime = runtime
        self._business = business
        self._context = DetectionContext(allowed_class_names, detection_threshold)
        self._evidence = evidence
        self._tracker = tracker or LightweightTracker(near_boundary=self._near_boundary_pixels)
        self._tracker.reset(session_id)
        self._crossing = crossing or CrossingEngine(uuid_factory)
        self._machine = machine or EventMachine(
            quiet_ms=round(zone.quiet_seconds * 1000),
            stable_ms=round(zone.stable_seconds * 1000),
            hard_idle_ms=round(zone.hard_idle_seconds * 1000),
        )
        self._uuid_factory = uuid_factory
        self._wall_now = wall_now
        self._monotonic_ns = monotonic_ns
        # ``_lock`` serializes analysis (held during inference); ``_status_lock`` guards the
        # cached event snapshot and metrics so other threads never wait on inference.
        self._lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._flags_lock = threading.Lock()
        self._pending = _Pending()
        self._pending_event_id = uuid_factory()
        self._timing = EventTiming()
        self._last_key: tuple[int, int] | None = None
        self._last_event_sequence = -1
        self._last_raw_ts: int | None = None
        self._last_adjusted_ts = 0
        self._pause_offset_ms = 0
        self._paused = False
        self._resume_gap_pending = False
        self._analyzed = 0
        self._late = 0
        self._detection_failures = 0
        self._latencies_ms: deque[float] = deque(maxlen=256)
        self._event_cache = self._machine.snapshot()

    # ------------------------------------------------------------------ geometry

    def _normalized(self, anchor: tuple[float, float]) -> tuple[float, float]:
        x = min(max(anchor[0] / self._zone.source_width, 0.0), 1.0)
        y = min(max(anchor[1] / self._zone.source_height, 0.0), 1.0)
        return x, y

    def _side(self, observation: TrackObservation) -> BoundarySide:
        return self._zone.boundary_side(self._normalized(observation.anchor_xy))

    def _near_boundary_pixels(self, anchor: tuple[float, float]) -> bool:
        return self._zone.boundary_side(self._normalized(anchor)) is BoundarySide.UNCERTAIN

    # ------------------------------------------------------------------ status

    @property
    def session_id(self) -> UUID:
        return self._session_id

    def active_event(self) -> EventSnapshot:
        """Latest event snapshot; never blocks on an in-flight inference."""
        with self._status_lock:
            return self._event_cache

    def _cache_event(self) -> None:
        snapshot = self._machine.snapshot()
        with self._status_lock:
            self._event_cache = snapshot

    def has_open_event(self) -> bool:
        return self.active_event().state in OPEN_EVENT_STATES

    @property
    def metrics(self) -> AnalyzerMetrics:
        with self._status_lock:
            latencies = sorted(self._latencies_ms)
            counts = (self._analyzed, self._late, self._detection_failures)
        mean = sum(latencies) / len(latencies) if latencies else None
        p95 = latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None
        return AnalyzerMetrics(*counts, mean, p95)

    # ------------------------------------------------------------------ signals (any thread)

    def notify_source_health(self, health: SourceHealth) -> None:
        with self._flags_lock:
            self._pending.health = health

    def notify_source_ended(self) -> None:
        with self._flags_lock:
            self._pending.source_ended = True

    def pause(self) -> None:
        with self._flags_lock:
            self._pending.paused = True
            self._pending.resumed = False

    def resume(self) -> None:
        with self._flags_lock:
            self._pending.paused = False
            self._pending.resumed = True

    @property
    def paused(self) -> bool:
        with self._flags_lock:
            return self._pending.paused

    # ------------------------------------------------------------------ analysis

    def process_pending(self) -> None:
        with self._lock:
            self._process_pending_locked()

    def _process_pending_locked(self) -> None:
        with self._flags_lock:
            health = self._pending.health
            ended = self._pending.source_ended
            resumed = self._pending.resumed
            self._pending.health = None
            self._pending.source_ended = False
            self._pending.resumed = False
        if resumed:
            self._resume_gap_pending = True
        if ended and self._evidence is not None:
            self._evidence.mark_source_ended()
        interrupted = {SourceHealth.RECONNECTING, SourceHealth.FAILED}
        interruption = health if health in interrupted else None
        if ended:
            # A file that ends mid-event cannot confirm settling; send the event to review.
            interruption = interruption or SourceHealth.FAILED
        if interruption is None or self._machine.snapshot().state not in OPEN_EVENT_STATES:
            return
        frame = EventFrame(
            frame_sequence=self._last_event_sequence + 1,
            source_timestamp_ms=self._last_adjusted_ts,
            motion_stable=False,
            source_health=interruption,
            unresolved_boundary_track_ids=frozenset(),
            actions=(),
            integrity_flags=frozenset(),
        )
        self._last_event_sequence += 1
        self._apply_locked(frame, self._last_raw_ts or 0)

    def analyze(
        self, frame: FrameEnvelope, health: SourceHealth = SourceHealth.CONNECTED
    ) -> EventSnapshot | None:
        """Analyze one sampled frame; returns the event snapshot or ``None`` if skipped."""
        with self._lock:
            self._process_pending_locked()
            key = (frame.continuity_segment, frame.sequence)
            if frame.session_id != self._session_id or (
                self._last_key is not None and key <= self._last_key
            ):
                self._discard_late_locked()
                return None
            try:
                batch = self._detector.detect(frame, self._context)
            except DetectionFailure as failure:
                self._detection_failed(f"{failure.code}: {failure.message}")
                self._last_key = key
                return None
            except Exception as error:
                self._detection_failed(type(error).__name__)
                self._last_key = key
                return None
            self._runtime.clear_alert("DETECTION_FAILED")
            self._last_key = key

            tracking = self._tracker.update(batch)
            crossing = self._crossing.update(self._pending_event_id, tracking, self._zone)
            raw_ts = frame.source_timestamp_ms
            if self._resume_gap_pending and self._last_raw_ts is not None:
                self._pause_offset_ms += max(0, raw_ts - self._last_raw_ts)
            self._resume_gap_pending = False
            adjusted = max(raw_ts - self._pause_offset_ms, self._last_adjusted_ts)

            live = [item for item in tracking.observations if item.state in LIVE_TRACK_STATES]
            speed_limit = STABLE_SPEED_NORM_PER_SECOND * self._zone.source_width
            motion_stable = all(
                math.hypot(*item.velocity_xy_per_second) <= speed_limit for item in live
            )
            unresolved = frozenset(
                item.track_id for item in live if self._side(item) is BoundarySide.UNCERTAIN
            )
            event_frame = EventFrame(
                frame_sequence=frame.sequence,
                source_timestamp_ms=adjusted,
                motion_stable=motion_stable,
                source_health=health,
                unresolved_boundary_track_ids=unresolved,
                actions=crossing.actions,
                integrity_flags=crossing.integrity_flags | tracking.integrity_flags,
            )
            self._last_event_sequence = frame.sequence
            self._last_raw_ts = raw_ts
            self._last_adjusted_ts = adjusted

            self._runtime.publish_analysis(
                AnalysisView(
                    session_id=self._session_id,
                    continuity_segment=frame.continuity_segment,
                    last_applied_frame_sequence=frame.sequence,
                    source_timestamp_ms=raw_ts,
                    detections=tuple(
                        DetectionView(
                            item.detection_id,
                            item.class_id,
                            item.class_name,
                            item.confidence,
                            item.bbox_xyxy,
                        )
                        for item in batch.detections
                    ),
                    tracks=tuple(
                        TrackView(
                            item.track_id,
                            item.class_name,
                            item.confidence,
                            item.bbox_xyxy,
                            item.state.value,
                            self._side(item).value,
                            item.ambiguous,
                        )
                        for item in tracking.observations
                    ),
                    actions=tuple(
                        ActionView(
                            item.action_id,
                            item.track_id,
                            item.action_type.value,
                            item.delta,
                            item.bbox_xyxy,
                            item.source_timestamp_ms,
                        )
                        for item in crossing.actions
                    ),
                )
            )
            self._runtime.update_source(source_timestamp_ms=raw_ts)
            snapshot = self._apply_locked(event_frame, raw_ts)
            latency_ms = max(0, self._monotonic_ns() - frame.captured_monotonic_ns) / 1_000_000
            with self._status_lock:
                self._analyzed += 1
                self._latencies_ms.append(latency_ms)
            return snapshot

    def _detection_failed(self, message: str) -> None:
        with self._status_lock:
            self._detection_failures += 1
        self._runtime.raise_alert(
            Alert("DETECTION_FAILED", "error", f"Detection failed: {message}")
        )

    def _discard_late_locked(self) -> None:
        with self._status_lock:
            self._late += 1
        metrics = self._runtime.snapshot().metrics
        self._runtime.update_metrics(late_results=metrics.late_results + 1)
        current = self._machine.snapshot()
        if current.state not in OPEN_EVENT_STATES:
            return
        # A frame at or below the machine's last sequence is recorded as a late result.
        flagged = self._machine.update(
            EventFrame(0, 0, False, SourceHealth.CONNECTED, frozenset(), (), frozenset()),
            self._event_context,
        )
        self._cache_event()
        self._runtime.update_event(
            event_view(flagged, last_action_at=self._timing.last_action_at),
            replace_only=flagged.event_id,
        )

    def _apply_locked(self, frame: EventFrame, raw_ts: int) -> EventSnapshot:
        before = self._machine.snapshot()
        snapshot = self._machine.update(frame, self._event_context)
        if snapshot.state is EventState.IDLE:
            # Flags seen while idle must not taint the next event.
            self._machine.reset()
            self._cache_event()
            return snapshot
        self._cache_event()
        now = self._wall_now()
        if before.state is EventState.IDLE:
            self._timing = EventTiming(started_at=now)
            if self._evidence is not None and snapshot.event_id is not None and snapshot.actions:
                first_action_ms = snapshot.actions[0].source_timestamp_ms
                self._evidence.begin_event(snapshot.event_id, first_action_ms)
        if len(snapshot.actions) != len(before.actions):
            self._timing = dataclasses.replace(self._timing, last_action_at=now)
        if snapshot.state not in TERMINAL_EVENT_STATES:
            self._runtime.update_event(
                event_view(snapshot, last_action_at=self._timing.last_action_at)
            )
            return snapshot

        timing = dataclasses.replace(self._timing, ended_at=now)
        self._runtime.update_event(event_view(snapshot, last_action_at=timing.last_action_at))
        if self._evidence is not None and snapshot.event_id is not None:
            with contextlib.suppress(KeyError):
                self._evidence.complete_event(snapshot.event_id, raw_ts)
        self._machine.reset()
        self._cache_event()
        self._pending_event_id = self._uuid_factory()
        self._timing = EventTiming()
        self._business.event_finished(snapshot, timing, await_evidence=self._evidence is not None)
        return snapshot

    def discard_open_event(self, reason: str) -> EventSnapshot | None:
        """Drop an incomplete event (stop with discard); returns it, or ``None`` if idle."""
        with self._lock:
            snapshot = self._machine.snapshot()
            if snapshot.state not in OPEN_EVENT_STATES or snapshot.event_id is None:
                return None
            if self._evidence is not None:
                with contextlib.suppress(KeyError):
                    self._evidence.abandon_event(snapshot.event_id, reason)
            self._machine.reset()
            self._cache_event()
            self._pending_event_id = self._uuid_factory()
            self._timing = EventTiming()
            self._runtime.update_event(EventView(), replace_only=snapshot.event_id)
            return snapshot

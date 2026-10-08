"""Immutable runtime snapshots shared by the analyzer, preview, status API and live stream.

The store keeps exactly one current :class:`RuntimeSnapshot`. Every change produces a new
frozen snapshot with a higher ``sequence`` so readers never observe partial updates. Live
subscribers hold at most one unsent snapshot (latest-only), matching the live-stream contract.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.vision.detector import FrameEnvelope

SCHEMA_VERSION = 1
BBox = tuple[float, float, float, float]

ACTIVE_EVENT_STATES = frozenset({"active", "settling"})


def iso_utc(value: datetime | None) -> str | None:
    """Format an aware datetime as ISO 8601 UTC with a ``Z`` suffix."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _bbox(value: BBox) -> list[float]:
    return [value[0], value[1], value[2], value[3]]


@dataclass(frozen=True, slots=True)
class Alert:
    code: str
    severity: str
    message: str

    def to_payload(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass(frozen=True, slots=True)
class MonitoringView:
    state: str = "stopped"
    session_id: UUID | None = None
    source_profile_id: str | None = None
    zone_profile_id: str | None = None
    task_id: str | None = None
    active_event_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SourceView:
    state: str = "closed"
    profile_id: str | None = None
    source_timestamp_ms: int | None = None
    continuity_segment: int = 0
    last_frame_at: datetime | None = None
    reconnect_attempts: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class DetectionView:
    detection_id: str
    class_id: int
    class_name: str
    confidence: float
    bbox: BBox

    def to_payload(self) -> dict[str, Any]:
        return {
            "detection_id": self.detection_id,
            "class_id": self.class_id,
            "class_name": self.class_name,
            "confidence": self.confidence,
            "bbox": _bbox(self.bbox),
        }


@dataclass(frozen=True, slots=True)
class TrackView:
    track_id: int
    class_name: str
    confidence: float
    bbox: BBox
    state: str
    line_side: str
    ambiguous: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "class_name": self.class_name,
            "confidence": self.confidence,
            "bbox": _bbox(self.bbox),
            "state": self.state,
            "line_side": self.line_side,
            "ambiguous": self.ambiguous,
        }


@dataclass(frozen=True, slots=True)
class ActionView:
    """A directional action drawn on the preview; not part of the live payload."""

    action_id: UUID
    track_id: int
    action_type: str
    delta: int
    bbox: BBox
    source_timestamp_ms: int


@dataclass(frozen=True, slots=True)
class AnalysisView:
    session_id: UUID | None = None
    continuity_segment: int = 0
    last_applied_frame_sequence: int | None = None
    source_timestamp_ms: int | None = None
    detections: tuple[DetectionView, ...] = ()
    tracks: tuple[TrackView, ...] = ()
    actions: tuple[ActionView, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "detections", tuple(self.detections))
        object.__setattr__(self, "tracks", tuple(self.tracks))
        object.__setattr__(self, "actions", tuple(self.actions))


@dataclass(frozen=True, slots=True)
class EventView:
    event_id: UUID | None = None
    state: str | None = None
    pick_count: int = 0
    return_count: int = 0
    net_quantity: int = 0
    last_action_at: datetime | None = None
    review_reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_id": None if self.event_id is None else str(self.event_id),
            "state": self.state,
            "pick_count": self.pick_count,
            "return_count": self.return_count,
            "net_quantity": self.net_quantity,
            "last_action_at": iso_utc(self.last_action_at),
            "review_reason": self.review_reason,
        }


@dataclass(frozen=True, slots=True)
class TaskView:
    task_id: str
    sku_id: str
    expected_quantity: int
    unit: str
    source_location_id: str
    status: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "sku_id": self.sku_id,
            "expected_quantity": self.expected_quantity,
            "unit": self.unit,
            "source_location_id": self.source_location_id,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class InventoryView:
    location_id: str
    sku_id: str
    quantity: int
    version: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "sku_id": self.sku_id,
            "quantity": self.quantity,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class MetricsView:
    capture_fps: float = 0.0
    analysis_fps: float = 0.0
    preview_fps: float = 0.0
    captured_frames: int = 0
    analyzed_frames: int = 0
    dropped_frames: int = 0
    late_results: int = 0
    result_latency_ms: float | None = None
    result_latency_p95_ms: float | None = None
    analysis_queue_depth: int = 0
    persistence_queue_depth: int = 0

    def to_live_payload(self) -> dict[str, Any]:
        return {
            "capture_fps": self.capture_fps,
            "analysis_fps": self.analysis_fps,
            "preview_fps": self.preview_fps,
            "dropped_frames": self.dropped_frames,
            "late_results": self.late_results,
            "result_latency_ms": self.result_latency_ms,
            "analysis_queue_depth": self.analysis_queue_depth,
            "persistence_queue_depth": self.persistence_queue_depth,
        }


@dataclass(frozen=True, slots=True)
class PersistenceView:
    state: str = "ready"
    last_commit_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PreviewImage:
    jpeg: bytes
    frame_sequence: int
    version: int
    session_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    sequence: int
    created_at: datetime
    monitoring: MonitoringView = field(default_factory=MonitoringView)
    source: SourceView = field(default_factory=SourceView)
    analysis: AnalysisView = field(default_factory=AnalysisView)
    event: EventView = field(default_factory=EventView)
    task: TaskView | None = None
    inventory: InventoryView | None = None
    metrics: MetricsView = field(default_factory=MetricsView)
    persistence: PersistenceView = field(default_factory=PersistenceView)
    alerts: tuple[Alert, ...] = ()

    def to_live_payload(self) -> dict[str, Any]:
        """Return a fresh dict shaped exactly like the live-stream ``payload``."""
        return {
            "monitoring_state": self.monitoring.state,
            "source": {
                "state": self.source.state,
                "profile_id": self.source.profile_id,
                "source_timestamp_ms": self.source.source_timestamp_ms,
                "continuity_segment": self.source.continuity_segment,
            },
            "analysis": {
                "last_applied_frame_sequence": self.analysis.last_applied_frame_sequence,
                "detections": [item.to_payload() for item in self.analysis.detections],
                "tracks": [item.to_payload() for item in self.analysis.tracks],
            },
            "event": self.event.to_payload(),
            "task": None if self.task is None else self.task.to_payload(),
            "inventory": None if self.inventory is None else self.inventory.to_payload(),
            "metrics": self.metrics.to_live_payload(),
            "alerts": [alert.to_payload() for alert in self.alerts],
        }

    def to_envelope(self, sent_at: datetime) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "schema_version": SCHEMA_VERSION,
            "sequence": self.sequence,
            "sent_at": iso_utc(sent_at),
            "payload": self.to_live_payload(),
        }


class SnapshotSubscription:
    """Latest-only mailbox: an unsent snapshot is replaced by a newer one."""

    def __init__(self, store: RuntimeStateStore, initial: RuntimeSnapshot) -> None:
        self._store = store
        self._condition = threading.Condition()
        self._pending: RuntimeSnapshot | None = initial
        self._closed = False
        self.dropped = 0

    @property
    def closed(self) -> bool:
        return self._closed

    def _offer(self, snapshot: RuntimeSnapshot) -> None:
        with self._condition:
            if self._closed:
                return
            if self._pending is not None:
                self.dropped += 1
            self._pending = snapshot
            self._condition.notify_all()

    def get(self, timeout: float | None = None) -> RuntimeSnapshot | None:
        """Return the newest unsent snapshot, or ``None`` on timeout or close."""
        with self._condition:
            if self._pending is None and not self._closed:
                self._condition.wait(timeout)
            if self._closed:
                return None
            snapshot, self._pending = self._pending, None
            return snapshot

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._pending = None
            self._condition.notify_all()
        self._store._unsubscribe(self)


class RuntimeStateStore:
    """Thread-safe owner of the current runtime snapshot, latest frame and preview JPEG."""

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._sequence = 0
        self._session_id: UUID | None = None
        self._snapshot = RuntimeSnapshot(sequence=0, created_at=self._now())
        self._subscribers: list[SnapshotSubscription] = []
        self._frame: FrameEnvelope | None = None
        self._preview_condition = threading.Condition(self._lock)
        self._preview: PreviewImage | None = None
        self._preview_version = 0

    # ------------------------------------------------------------------ reads

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return self._snapshot

    @property
    def session_id(self) -> UUID | None:
        with self._lock:
            return self._session_id

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def subscribe(self) -> SnapshotSubscription:
        with self._lock:
            subscription = SnapshotSubscription(self, self._snapshot)
            self._subscribers.append(subscription)
            return subscription

    def _unsubscribe(self, subscription: SnapshotSubscription) -> None:
        with self._lock:
            if subscription in self._subscribers:
                self._subscribers.remove(subscription)

    # ------------------------------------------------------------------ writes

    def _commit(self, **changes: Any) -> RuntimeSnapshot:
        """Publish a new snapshot; caller must hold the lock."""
        self._sequence += 1
        snapshot = dataclasses.replace(
            self._snapshot, sequence=self._sequence, created_at=self._now(), **changes
        )
        self._snapshot = snapshot
        for subscription in tuple(self._subscribers):
            subscription._offer(snapshot)
        return snapshot

    def update_monitoring(self, **changes: Any) -> RuntimeSnapshot:
        with self._lock:
            monitoring = dataclasses.replace(self._snapshot.monitoring, **changes)
            return self._commit(monitoring=monitoring)

    def update_source(self, **changes: Any) -> RuntimeSnapshot:
        with self._lock:
            return self._commit(source=dataclasses.replace(self._snapshot.source, **changes))

    def update_metrics(self, **changes: Any) -> RuntimeSnapshot:
        with self._lock:
            return self._commit(metrics=dataclasses.replace(self._snapshot.metrics, **changes))

    def update_persistence(self, **changes: Any) -> RuntimeSnapshot:
        with self._lock:
            persistence = dataclasses.replace(self._snapshot.persistence, **changes)
            return self._commit(persistence=persistence)

    def update_task(self, task: TaskView | None) -> RuntimeSnapshot:
        with self._lock:
            return self._commit(task=task)

    def update_inventory(self, inventory: InventoryView | None) -> RuntimeSnapshot:
        with self._lock:
            return self._commit(inventory=inventory)

    def update_event(
        self, event: EventView, *, replace_only: UUID | None = None
    ) -> RuntimeSnapshot | None:
        """Show ``event``; with ``replace_only`` only if that event is still displayed."""
        with self._lock:
            if replace_only is not None and self._snapshot.event.event_id != replace_only:
                return None
            active = event.event_id if event.state in ACTIVE_EVENT_STATES else None
            monitoring = dataclasses.replace(self._snapshot.monitoring, active_event_id=active)
            return self._commit(event=event, monitoring=monitoring)

    def raise_alert(self, alert: Alert) -> RuntimeSnapshot:
        """Add or replace the alert with the same code."""
        with self._lock:
            alerts = tuple(item for item in self._snapshot.alerts if item.code != alert.code)
            return self._commit(alerts=(*alerts, alert))

    def clear_alert(self, code: str) -> RuntimeSnapshot | None:
        with self._lock:
            if all(item.code != code for item in self._snapshot.alerts):
                return None
            alerts = tuple(item for item in self._snapshot.alerts if item.code != code)
            return self._commit(alerts=alerts)

    def has_alert(self, code: str) -> bool:
        with self._lock:
            return any(item.code == code for item in self._snapshot.alerts)

    def _accepts_session(self, session_id: UUID | None) -> bool:
        if self._session_id is None:
            self._session_id = session_id
            return True
        return session_id == self._session_id

    def publish_analysis(self, analysis: AnalysisView) -> bool:
        """Apply an ordered analysis result; stale or foreign results are rejected.

        Results from another session, from an older continuity segment, or with a frame
        sequence not greater than the last applied one in the same segment are discarded
        and counted in ``metrics.late_results``.
        """
        with self._lock:
            current = self._snapshot.analysis
            sequence = analysis.last_applied_frame_sequence
            stale = (
                sequence is None
                or not self._accepts_session(analysis.session_id)
                or analysis.continuity_segment < current.continuity_segment
                or (
                    analysis.continuity_segment == current.continuity_segment
                    and current.last_applied_frame_sequence is not None
                    and sequence <= current.last_applied_frame_sequence
                )
            )
            if stale:
                metrics = self._snapshot.metrics
                self._commit(
                    metrics=dataclasses.replace(metrics, late_results=metrics.late_results + 1)
                )
                return False
            source = dataclasses.replace(
                self._snapshot.source, continuity_segment=analysis.continuity_segment
            )
            self._commit(analysis=analysis, source=source)
            return True

    def publish_frame(self, frame: FrameEnvelope) -> bool:
        """Keep the newest source frame of the current session for preview rendering."""
        with self._lock:
            if not self._accepts_session(frame.session_id):
                return False
            current = self._frame
            if current is not None and (frame.continuity_segment, frame.sequence) <= (
                current.continuity_segment,
                current.sequence,
            ):
                return False
            self._frame = frame
            return True

    def latest_frame(self) -> FrameEnvelope | None:
        with self._lock:
            return self._frame

    def publish_preview(self, jpeg: bytes, frame_sequence: int) -> bool:
        with self._preview_condition:
            current = self._preview
            if current is not None and frame_sequence <= current.frame_sequence:
                return False
            self._preview_version += 1
            self._preview = PreviewImage(
                jpeg, frame_sequence, self._preview_version, self._session_id
            )
            self._preview_condition.notify_all()
            return True

    def preview(self) -> PreviewImage | None:
        with self._lock:
            return self._preview

    def wait_preview(self, after_version: int, timeout: float | None) -> PreviewImage | None:
        """Block until a preview newer than ``after_version`` exists or ``timeout`` passes."""
        with self._preview_condition:
            self._preview_condition.wait_for(
                lambda: self._preview is not None and self._preview.version > after_version,
                timeout,
            )
            preview = self._preview
            if preview is None or preview.version <= after_version:
                return None
            return preview

    def reset_session(
        self,
        session_id: UUID | None,
        *,
        monitoring: Mapping[str, Any] | None = None,
        source: Mapping[str, Any] | None = None,
    ) -> RuntimeSnapshot:
        """Start a new session view: clear analysis, event, frame, preview and counters."""
        with self._preview_condition:
            self._session_id = session_id
            self._frame = None
            self._preview = None
            self._preview_condition.notify_all()
            monitoring_view = MonitoringView(
                **{
                    "state": self._snapshot.monitoring.state,
                    "session_id": session_id,
                    **(monitoring or {}),
                }
            )
            return self._commit(
                monitoring=monitoring_view,
                source=SourceView(**(source or {})),
                analysis=AnalysisView(session_id=session_id),
                event=EventView(),
                metrics=MetricsView(),
                alerts=(),
            )

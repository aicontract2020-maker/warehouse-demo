from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.domain.crossing import ActionType, DirectionalAction


class SourceHealth(StrEnum):
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ENDED = "ended"
    FAILED = "failed"


class EventState(StrEnum):
    IDLE = "idle"
    ACTIVE = "active"
    SETTLING = "settling"
    COMPLETED = "completed"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class EventContext:
    task_id: str
    zone_profile_id: str
    zone_version: int
    sku_id: str
    unit: str
    source_location_id: str
    destination_location_id: str
    source_session_id: UUID
    configuration_version: str


@dataclass(frozen=True, slots=True)
class EventFrame:
    frame_sequence: int
    source_timestamp_ms: int
    motion_stable: bool
    source_health: SourceHealth
    unresolved_boundary_track_ids: frozenset[int]
    actions: tuple[DirectionalAction, ...]
    integrity_flags: frozenset[str]

    def __post_init__(self) -> None:
        if self.frame_sequence < 0 or self.source_timestamp_ms < 0:
            raise ValueError("frame sequence and source timestamp must be non-negative")
        if any(track_id <= 0 for track_id in self.unresolved_boundary_track_ids):
            raise ValueError("unresolved track IDs must be positive")


@dataclass(frozen=True, slots=True)
class EventSnapshot:
    event_id: UUID | None
    state: EventState
    started_source_timestamp_ms: int | None
    ended_source_timestamp_ms: int | None
    pick_count: int
    return_count: int
    net_quantity: int
    actions: tuple[DirectionalAction, ...]
    aggregate_confidence: float | None
    integrity_flags: frozenset[str]
    review_reason: str | None
    context: EventContext | None


class EventMachine:
    def __init__(
        self,
        quiet_ms: int = 3_000,
        stable_ms: int = 2_000,
        hard_idle_ms: int = 10_000,
    ) -> None:
        if quiet_ms <= 0 or stable_ms <= 0 or hard_idle_ms < quiet_ms + stable_ms:
            raise ValueError("event timers are inconsistent")
        self._quiet_ms = quiet_ms
        self._stable_ms = stable_ms
        self._hard_idle_ms = hard_idle_ms
        self.reset()

    def reset(self) -> None:
        self._event_id: UUID | None = None
        self._state = EventState.IDLE
        self._started_source_timestamp_ms: int | None = None
        self._ended_source_timestamp_ms: int | None = None
        self._last_action_timestamp_ms: int | None = None
        self._stable_since_ms: int | None = None
        self._actions: list[DirectionalAction] = []
        self._action_keys: set[tuple[UUID, int, int]] = set()
        self._integrity_flags: set[str] = set()
        self._review_reason: str | None = None
        self._context: EventContext | None = None
        self._last_frame_sequence = -1
        self._last_source_timestamp_ms = -1

    def update(self, frame: EventFrame, context: EventContext) -> EventSnapshot:
        if self._state in {
            EventState.COMPLETED,
            EventState.REVIEW_REQUIRED,
            EventState.APPROVED,
            EventState.REJECTED,
        }:
            return self.snapshot()

        if (
            frame.frame_sequence <= self._last_frame_sequence
            or frame.source_timestamp_ms < self._last_source_timestamp_ms
        ):
            self._integrity_flags.add("LATE_RESULT_DISCARDED")
            return self.snapshot()

        self._last_frame_sequence = frame.frame_sequence
        self._last_source_timestamp_ms = frame.source_timestamp_ms
        self._integrity_flags.update(frame.integrity_flags)

        if self._state is not EventState.IDLE and self._context != context:
            raise ValueError("event context cannot change while an event is active")

        new_actions = self._new_actions(frame.actions)
        if self._state is EventState.IDLE:
            if not new_actions:
                return self.snapshot()
            self._start(new_actions, frame.source_timestamp_ms, context)
        elif new_actions:
            self._append_actions(new_actions, frame.source_timestamp_ms)

        if self._net_quantity() < 0:
            self._finish_for_review(frame.source_timestamp_ms, "NEGATIVE_NET_QUANTITY")
            return self.snapshot()

        if frame.source_health in {SourceHealth.RECONNECTING, SourceHealth.FAILED}:
            self._integrity_flags.add("SOURCE_INTERRUPTED")
            self._finish_for_review(frame.source_timestamp_ms, "SOURCE_INTERRUPTED")
            return self.snapshot()

        if new_actions:
            return self.snapshot()

        assert self._last_action_timestamp_ms is not None
        idle_ms = frame.source_timestamp_ms - self._last_action_timestamp_ms
        if idle_ms >= self._hard_idle_ms:
            self._finish_for_review(frame.source_timestamp_ms, "HARD_IDLE_TIMEOUT")
            return self.snapshot()

        if self._state is EventState.ACTIVE and idle_ms >= self._quiet_ms:
            self._state = EventState.SETTLING
            self._stable_since_ms = None

        if self._state is EventState.SETTLING:
            stable_now = frame.motion_stable and not frame.unresolved_boundary_track_ids
            if not stable_now:
                self._stable_since_ms = None
            elif self._stable_since_ms is None:
                self._stable_since_ms = frame.source_timestamp_ms
            elif frame.source_timestamp_ms - self._stable_since_ms >= self._stable_ms:
                self._state = EventState.COMPLETED
                self._ended_source_timestamp_ms = frame.source_timestamp_ms

        return self.snapshot()

    def snapshot(self) -> EventSnapshot:
        pick_count = sum(
            item.action_type is ActionType.PICK for item in self._actions
        )
        return_count = sum(
            item.action_type is ActionType.RETURN for item in self._actions
        )
        confidence = (
            min(item.confidence for item in self._actions) if self._actions else None
        )
        return EventSnapshot(
            event_id=self._event_id,
            state=self._state,
            started_source_timestamp_ms=self._started_source_timestamp_ms,
            ended_source_timestamp_ms=self._ended_source_timestamp_ms,
            pick_count=pick_count,
            return_count=return_count,
            net_quantity=pick_count - return_count,
            actions=tuple(self._actions),
            aggregate_confidence=confidence,
            integrity_flags=frozenset(self._integrity_flags),
            review_reason=self._review_reason,
            context=self._context,
        )

    def apply_outcome(
        self, state: EventState, review_reason: str | None = None
    ) -> EventSnapshot:
        allowed = {
            EventState.COMPLETED: {EventState.APPROVED, EventState.REVIEW_REQUIRED},
            EventState.REVIEW_REQUIRED: {EventState.APPROVED, EventState.REJECTED},
        }
        if state not in allowed.get(self._state, set()):
            raise ValueError(f"invalid event transition: {self._state} -> {state}")
        self._state = state
        self._review_reason = review_reason
        return self.snapshot()

    def _new_actions(
        self, actions: tuple[DirectionalAction, ...]
    ) -> tuple[DirectionalAction, ...]:
        new_actions: list[DirectionalAction] = []
        for item in sorted(actions, key=lambda value: value.action_sequence):
            key = (item.event_id, item.track_id, item.transition_index)
            if key not in self._action_keys:
                new_actions.append(item)
        return tuple(new_actions)

    def _start(
        self,
        actions: tuple[DirectionalAction, ...],
        source_timestamp_ms: int,
        context: EventContext,
    ) -> None:
        event_ids = {item.event_id for item in actions}
        if len(event_ids) != 1:
            raise ValueError("one frame cannot start multiple events")
        self._event_id = next(iter(event_ids))
        self._state = EventState.ACTIVE
        self._started_source_timestamp_ms = source_timestamp_ms
        self._context = context
        self._append_actions(actions, source_timestamp_ms)

    def _append_actions(
        self, actions: tuple[DirectionalAction, ...], source_timestamp_ms: int
    ) -> None:
        if any(item.event_id != self._event_id for item in actions):
            raise ValueError("action event ID does not match active event")
        for item in actions:
            key = (item.event_id, item.track_id, item.transition_index)
            if key in self._action_keys:
                continue
            self._action_keys.add(key)
            self._actions.append(item)
        self._actions.sort(key=lambda value: value.action_sequence)
        self._last_action_timestamp_ms = source_timestamp_ms
        self._stable_since_ms = None
        self._state = EventState.ACTIVE

    def _net_quantity(self) -> int:
        return sum(item.delta for item in self._actions)

    def _finish_for_review(self, timestamp_ms: int, reason: str) -> None:
        self._state = EventState.REVIEW_REQUIRED
        self._ended_source_timestamp_ms = timestamp_ms
        self._review_reason = reason


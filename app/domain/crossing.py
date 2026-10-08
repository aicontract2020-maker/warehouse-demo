from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid4

from app.domain.models import BoundarySide
from app.domain.zones import ZoneProfile
from app.vision.tracker import TrackingBatch, TrackState


class ActionType(StrEnum):
    PICK = "pick"
    RETURN = "return"


@dataclass(frozen=True, slots=True)
class DirectionalAction:
    action_id: UUID
    event_id: UUID
    action_sequence: int
    track_id: int
    transition_index: int
    action_type: ActionType
    delta: int
    source_timestamp_ms: int
    frame_sequence: int
    confidence: float
    bbox_xyxy: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class CrossingResult:
    actions: tuple[DirectionalAction, ...]
    integrity_flags: frozenset[str]


@dataclass(slots=True)
class _TrackCrossingState:
    stable_side: BoundarySide | None = None
    candidate_side: BoundarySide | None = None
    candidate_observations: int = 0
    transition_index: int = 0
    outstanding_picks: int = 0


class CrossingEngine:
    def __init__(self, uuid_factory: Callable[[], UUID] = uuid4) -> None:
        self._uuid_factory = uuid_factory
        self._event_id: UUID | None = None
        self._action_sequence = 0
        self._track_states: dict[int, _TrackCrossingState] = {}

    def reset(self, event_id: UUID) -> None:
        self._event_id = event_id
        self._action_sequence = 0
        self._track_states.clear()

    def update(
        self,
        event_id: UUID,
        batch: TrackingBatch,
        zone: ZoneProfile,
    ) -> CrossingResult:
        if self._event_id != event_id:
            self.reset(event_id)

        actions: list[DirectionalAction] = []
        integrity_flags = set(batch.integrity_flags)
        for observation in sorted(batch.observations, key=lambda item: item.track_id):
            state = self._track_states.setdefault(
                observation.track_id, _TrackCrossingState()
            )
            normalized_anchor = (
                observation.anchor_xy[0] / zone.source_width,
                observation.anchor_xy[1] / zone.source_height,
            )
            side = zone.boundary_side(normalized_anchor)

            if observation.state in {TrackState.LOST, TrackState.EXPIRED}:
                state.candidate_side = None
                state.candidate_observations = 0
                if side is BoundarySide.UNCERTAIN or observation.ambiguous:
                    integrity_flags.add("TRACK_LOST_NEAR_BOUNDARY")
                continue
            if observation.state is not TrackState.CONFIRMED or observation.ambiguous:
                state.candidate_side = None
                state.candidate_observations = 0
                continue
            if side is BoundarySide.UNCERTAIN:
                state.candidate_side = None
                state.candidate_observations = 0
                continue

            if state.stable_side is None:
                state.stable_side = side
                continue
            if side is state.stable_side:
                state.candidate_side = None
                state.candidate_observations = 0
                continue

            if state.candidate_side is side:
                state.candidate_observations += 1
            else:
                state.candidate_side = side
                state.candidate_observations = 1
            if state.candidate_observations < zone.crossing_confirm_frames:
                continue

            action_type: ActionType | None = None
            if state.stable_side is BoundarySide.SHELF and side is BoundarySide.EXIT:
                action_type = ActionType.PICK
                state.outstanding_picks += 1
            elif (
                state.stable_side is BoundarySide.EXIT
                and side is BoundarySide.SHELF
                and state.outstanding_picks > 0
            ):
                action_type = ActionType.RETURN
                state.outstanding_picks -= 1

            state.stable_side = side
            state.candidate_side = None
            state.candidate_observations = 0
            if action_type is None:
                continue

            state.transition_index += 1
            self._action_sequence += 1
            actions.append(
                DirectionalAction(
                    action_id=self._uuid_factory(),
                    event_id=event_id,
                    action_sequence=self._action_sequence,
                    track_id=observation.track_id,
                    transition_index=state.transition_index,
                    action_type=action_type,
                    delta=1 if action_type is ActionType.PICK else -1,
                    source_timestamp_ms=batch.source_timestamp_ms,
                    frame_sequence=batch.frame_sequence,
                    confidence=observation.confidence,
                    bbox_xyxy=observation.bbox_xyxy,
                )
            )

        return CrossingResult(
            actions=tuple(actions), integrity_flags=frozenset(integrity_flags)
        )


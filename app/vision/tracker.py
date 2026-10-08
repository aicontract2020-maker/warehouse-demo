from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.vision.detector import Detection, DetectionBatch

BBox = tuple[float, float, float, float]
Point = tuple[float, float]


class TrackState(StrEnum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    LOST = "lost"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    confirmation_observations: int = 2
    max_missed_duration_ms: int = 750
    minimum_iou: float = 0.05
    base_centroid_gate_pixels: float = 80.0
    max_speed_pixels_per_second: float = 800.0
    overlap_iou_threshold: float = 0.8

    def __post_init__(self) -> None:
        if self.confirmation_observations < 2:
            raise ValueError("confirmation_observations must be at least two")
        if self.max_missed_duration_ms <= 0:
            raise ValueError("max_missed_duration_ms must be positive")
        if not 0 <= self.minimum_iou <= 1 or not 0 <= self.overlap_iou_threshold <= 1:
            raise ValueError("IoU thresholds must be in [0, 1]")
        if self.base_centroid_gate_pixels <= 0 or self.max_speed_pixels_per_second <= 0:
            raise ValueError("centroid gates must be positive")


@dataclass(frozen=True, slots=True)
class TrackObservation:
    track_id: int
    class_name: str
    confidence: float
    bbox_xyxy: BBox
    anchor_xy: Point
    velocity_xy_per_second: Point
    state: TrackState
    age_observations: int
    missed_duration_ms: int
    ambiguous: bool


@dataclass(frozen=True, slots=True)
class TrackingBatch:
    session_id: UUID
    continuity_segment: int
    frame_sequence: int
    source_timestamp_ms: int
    observations: tuple[TrackObservation, ...]
    late_result_discarded: bool = False
    integrity_flags: frozenset[str] = frozenset()


@dataclass(slots=True)
class _Track:
    track_id: int
    class_name: str
    confidence: float
    bbox_xyxy: BBox
    anchor_xy: Point
    velocity_xy_per_second: Point
    state: TrackState
    age_observations: int
    last_timestamp_ms: int
    ambiguous: bool

    def observation(
        self, timestamp_ms: int, state: TrackState | None = None
    ) -> TrackObservation:
        current_state = state or self.state
        missed_duration = max(0, timestamp_ms - self.last_timestamp_ms)
        if current_state in {TrackState.TENTATIVE, TrackState.CONFIRMED}:
            missed_duration = 0
        return TrackObservation(
            track_id=self.track_id,
            class_name=self.class_name,
            confidence=self.confidence,
            bbox_xyxy=self.bbox_xyxy,
            anchor_xy=self.anchor_xy,
            velocity_xy_per_second=self.velocity_xy_per_second,
            state=current_state,
            age_observations=self.age_observations,
            missed_duration_ms=missed_duration,
            ambiguous=self.ambiguous,
        )


def _anchor(bbox: BBox) -> Point:
    left, _, right, bottom = bbox
    return ((left + right) / 2, bottom)


def _iou(first: BBox, second: BBox) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    if intersection == 0:
        return 0.0
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    return intersection / (first_area + second_area - intersection)


class LightweightTracker:
    """Deterministic greedy tracker for a small number of independently visible goods."""

    def __init__(
        self,
        config: TrackerConfig | None = None,
        near_boundary: Callable[[Point], bool] | None = None,
    ) -> None:
        self._config = config or TrackerConfig()
        self._near_boundary = near_boundary or (lambda _anchor: False)
        self._session_id: UUID | None = None
        self._continuity_segment = 0
        self._last_sequence = -1
        self._last_timestamp_ms = -1
        self._next_track_id = 1
        self._tracks: dict[int, _Track] = {}
        self._pending_discontinuity: int | None = None

    def reset(self, session_id: UUID) -> None:
        self._session_id = session_id
        self._continuity_segment = 0
        self._last_sequence = -1
        self._last_timestamp_ms = -1
        self._next_track_id = 1
        self._tracks.clear()
        self._pending_discontinuity = None

    def mark_discontinuity(self, continuity_segment: int) -> None:
        if continuity_segment <= self._continuity_segment:
            raise ValueError("continuity segment must increase")
        self._pending_discontinuity = continuity_segment

    def update(self, batch: DetectionBatch) -> TrackingBatch:
        if self._session_id is None:
            self._session_id = batch.session_id
        elif batch.session_id != self._session_id:
            raise ValueError("tracker must be reset before accepting a new session")

        if (
            batch.frame_sequence <= self._last_sequence
            or batch.source_timestamp_ms < self._last_timestamp_ms
            or batch.continuity_segment < self._continuity_segment
        ):
            return TrackingBatch(
                session_id=batch.session_id,
                continuity_segment=batch.continuity_segment,
                frame_sequence=batch.frame_sequence,
                source_timestamp_ms=batch.source_timestamp_ms,
                observations=(),
                late_result_discarded=True,
                integrity_flags=frozenset({"LATE_RESULT_DISCARDED"}),
            )

        observations: list[TrackObservation] = []
        integrity_flags: set[str] = set()
        if self._is_discontinuous(batch.continuity_segment):
            for track in sorted(self._tracks.values(), key=lambda value: value.track_id):
                track.state = TrackState.EXPIRED
                track.ambiguous = True
                observations.append(track.observation(batch.source_timestamp_ms))
            self._tracks.clear()
            self._continuity_segment = batch.continuity_segment
            self._pending_discontinuity = None
            integrity_flags.add("SOURCE_INTERRUPTED")

        overlapping_detection_indexes = self._overlapping_detection_indexes(batch.detections)
        if overlapping_detection_indexes:
            integrity_flags.add("OVERLAPPING_INSEPARABLE")

        matches = self._associate(batch)
        matched_tracks = {track_id for track_id, _ in matches}
        matched_detections = {detection_index for _, detection_index in matches}

        for track_id, detection_index in matches:
            track = self._tracks[track_id]
            detection = batch.detections[detection_index]
            new_anchor = _anchor(detection.bbox_xyxy)
            elapsed_seconds = max(
                (batch.source_timestamp_ms - track.last_timestamp_ms) / 1000,
                1e-9,
            )
            track.velocity_xy_per_second = (
                (new_anchor[0] - track.anchor_xy[0]) / elapsed_seconds,
                (new_anchor[1] - track.anchor_xy[1]) / elapsed_seconds,
            )
            track.anchor_xy = new_anchor
            track.bbox_xyxy = detection.bbox_xyxy
            track.confidence = detection.confidence
            track.last_timestamp_ms = batch.source_timestamp_ms
            track.age_observations += 1
            track.state = (
                TrackState.CONFIRMED
                if track.age_observations >= self._config.confirmation_observations
                else TrackState.TENTATIVE
            )
            track.ambiguous = track.ambiguous or detection_index in overlapping_detection_indexes
            observations.append(track.observation(batch.source_timestamp_ms))

        expired_track_ids: list[int] = []
        for track_id, track in sorted(self._tracks.items()):
            if track_id in matched_tracks:
                continue
            missed_duration = batch.source_timestamp_ms - track.last_timestamp_ms
            track.state = (
                TrackState.EXPIRED
                if missed_duration > self._config.max_missed_duration_ms
                else TrackState.LOST
            )
            if self._near_boundary(track.anchor_xy):
                track.ambiguous = True
                integrity_flags.add("TRACK_LOST_NEAR_BOUNDARY")
            observations.append(track.observation(batch.source_timestamp_ms))
            if track.state is TrackState.EXPIRED:
                expired_track_ids.append(track_id)

        for track_id in expired_track_ids:
            del self._tracks[track_id]

        for detection_index, detection in enumerate(batch.detections):
            if detection_index in matched_detections:
                continue
            track = _Track(
                track_id=self._next_track_id,
                class_name=detection.class_name,
                confidence=detection.confidence,
                bbox_xyxy=detection.bbox_xyxy,
                anchor_xy=_anchor(detection.bbox_xyxy),
                velocity_xy_per_second=(0.0, 0.0),
                state=TrackState.TENTATIVE,
                age_observations=1,
                last_timestamp_ms=batch.source_timestamp_ms,
                ambiguous=detection_index in overlapping_detection_indexes,
            )
            self._tracks[track.track_id] = track
            self._next_track_id += 1
            observations.append(track.observation(batch.source_timestamp_ms))

        self._last_sequence = batch.frame_sequence
        self._last_timestamp_ms = batch.source_timestamp_ms
        return TrackingBatch(
            session_id=batch.session_id,
            continuity_segment=batch.continuity_segment,
            frame_sequence=batch.frame_sequence,
            source_timestamp_ms=batch.source_timestamp_ms,
            observations=tuple(sorted(observations, key=lambda value: value.track_id)),
            integrity_flags=frozenset(integrity_flags),
        )

    def _is_discontinuous(self, batch_segment: int) -> bool:
        pending = self._pending_discontinuity
        return batch_segment > self._continuity_segment or (
            pending is not None and batch_segment >= pending
        )

    def _overlapping_detection_indexes(
        self, detections: tuple[Detection, ...]
    ) -> set[int]:
        overlapping: set[int] = set()
        for first_index, first in enumerate(detections):
            for second_index in range(first_index + 1, len(detections)):
                second = detections[second_index]
                if (
                    first.class_name == second.class_name
                    and _iou(first.bbox_xyxy, second.bbox_xyxy)
                    >= self._config.overlap_iou_threshold
                ):
                    overlapping.update((first_index, second_index))
        return overlapping

    def _associate(self, batch: DetectionBatch) -> tuple[tuple[int, int], ...]:
        candidates: list[tuple[float, int, int]] = []
        for track_id, track in self._tracks.items():
            elapsed_seconds = max(
                (batch.source_timestamp_ms - track.last_timestamp_ms) / 1000,
                0.0,
            )
            predicted_anchor = (
                track.anchor_xy[0] + track.velocity_xy_per_second[0] * elapsed_seconds,
                track.anchor_xy[1] + track.velocity_xy_per_second[1] * elapsed_seconds,
            )
            gate = (
                self._config.base_centroid_gate_pixels
                + self._config.max_speed_pixels_per_second * elapsed_seconds
            )
            for detection_index, detection in enumerate(batch.detections):
                if detection.class_name != track.class_name:
                    continue
                detection_anchor = _anchor(detection.bbox_xyxy)
                distance = math.dist(predicted_anchor, detection_anchor)
                overlap = _iou(track.bbox_xyxy, detection.bbox_xyxy)
                if overlap < self._config.minimum_iou and distance > gate:
                    continue
                score = overlap * 2 + max(0.0, 1 - distance / gate)
                candidates.append((-score, track_id, detection_index))

        matched_tracks: set[int] = set()
        matched_detections: set[int] = set()
        matches: list[tuple[int, int]] = []
        for _, track_id, detection_index in sorted(candidates):
            if track_id in matched_tracks or detection_index in matched_detections:
                continue
            matched_tracks.add(track_id)
            matched_detections.add(detection_index)
            matches.append((track_id, detection_index))
        return tuple(matches)


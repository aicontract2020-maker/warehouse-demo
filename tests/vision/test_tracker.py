from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.vision.detector import Detection, DetectionBatch
from app.vision.tracker import LightweightTracker, TrackerConfig, TrackState


def batch(
    session_id: UUID,
    sequence: int,
    timestamp_ms: int,
    detections: tuple[Detection, ...],
    continuity_segment: int = 0,
) -> DetectionBatch:
    return DetectionBatch(
        session_id=session_id,
        continuity_segment=continuity_segment,
        frame_sequence=sequence,
        source_timestamp_ms=timestamp_ms,
        started_monotonic_ns=timestamp_ms * 1_000_000,
        completed_monotonic_ns=timestamp_ms * 1_000_000 + 1,
        model_id="fixture",
        detections=detections,
    )


def item(
    sequence: int,
    ordinal: int,
    bbox: tuple[float, float, float, float],
    class_name: str = "box",
    confidence: float = 0.95,
) -> Detection:
    return Detection(
        detection_id=f"{sequence}:{ordinal}",
        class_id=0,
        class_name=class_name,
        confidence=confidence,
        bbox_xyxy=bbox,
    )


def test_track_requires_two_observations_and_retains_stable_id() -> None:
    session_id = uuid4()
    tracker = LightweightTracker()

    first = tracker.update(batch(session_id, 1, 0, (item(1, 0, (10, 10, 110, 110)),)))
    second = tracker.update(batch(session_id, 2, 250, (item(2, 0, (20, 10, 120, 110)),)))

    assert first.observations[0].track_id == 1
    assert first.observations[0].state is TrackState.TENTATIVE
    assert second.observations[0].track_id == 1
    assert second.observations[0].state is TrackState.CONFIRMED
    assert second.observations[0].age_observations == 2
    assert second.observations[0].anchor_xy == (70.0, 110)
    assert second.observations[0].velocity_xy_per_second == (40.0, 0.0)


def test_tracker_uses_velocity_gate_when_fast_motion_has_no_iou() -> None:
    session_id = uuid4()
    tracker = LightweightTracker(
        TrackerConfig(base_centroid_gate_pixels=40, max_speed_pixels_per_second=1000)
    )

    tracker.update(batch(session_id, 1, 0, (item(1, 0, (0, 0, 30, 30)),)))
    result = tracker.update(batch(session_id, 2, 250, (item(2, 0, (120, 0, 150, 30)),)))

    assert result.observations[0].track_id == 1
    assert result.observations[0].state is TrackState.CONFIRMED


def test_tracker_keeps_three_separable_goods_distinct_and_class_consistent() -> None:
    session_id = uuid4()
    tracker = LightweightTracker()
    first_items = (
        item(1, 0, (0, 0, 40, 40), "box"),
        item(1, 1, (100, 0, 140, 40), "bag"),
        item(1, 2, (200, 0, 240, 40), "box"),
    )
    second_items = (
        item(2, 0, (10, 0, 50, 40), "box"),
        item(2, 1, (110, 0, 150, 40), "bag"),
        item(2, 2, (210, 0, 250, 40), "box"),
    )

    tracker.update(batch(session_id, 1, 0, first_items))
    result = tracker.update(batch(session_id, 2, 250, second_items))

    assert [(track.track_id, track.class_name) for track in result.observations] == [
        (1, "box"),
        (2, "bag"),
        (3, "box"),
    ]
    assert all(track.state is TrackState.CONFIRMED for track in result.observations)


def test_overlapping_goods_are_marked_ambiguous_not_merged_to_expected_count() -> None:
    session_id = uuid4()
    tracker = LightweightTracker(TrackerConfig(overlap_iou_threshold=0.7))
    detections = (
        item(1, 0, (0, 0, 100, 100)),
        item(1, 1, (5, 5, 105, 105)),
    )

    result = tracker.update(batch(session_id, 1, 0, detections))

    assert len(result.observations) == 2
    assert all(track.ambiguous for track in result.observations)
    assert "OVERLAPPING_INSEPARABLE" in result.integrity_flags


def test_lost_track_near_boundary_is_ambiguous_and_id_is_not_reused() -> None:
    session_id = uuid4()
    tracker = LightweightTracker(
        TrackerConfig(max_missed_duration_ms=500),
        near_boundary=lambda anchor: 45 <= anchor[0] <= 55,
    )
    tracker.update(batch(session_id, 1, 0, (item(1, 0, (0, 0, 100, 100)),)))
    tracker.update(batch(session_id, 2, 250, (item(2, 0, (0, 0, 100, 100)),)))

    lost = tracker.update(batch(session_id, 3, 500, ()))
    expired = tracker.update(batch(session_id, 4, 1_000, ()))
    new_track = tracker.update(batch(session_id, 5, 1_250, (item(5, 0, (0, 0, 100, 100)),)))

    assert lost.observations[0].state is TrackState.LOST
    assert lost.observations[0].ambiguous is True
    assert "TRACK_LOST_NEAR_BOUNDARY" in lost.integrity_flags
    assert expired.observations[0].state is TrackState.EXPIRED
    assert new_track.observations[0].track_id == 2


def test_late_batch_is_discarded_without_mutating_tracks() -> None:
    session_id = uuid4()
    tracker = LightweightTracker()
    accepted = tracker.update(batch(session_id, 2, 500, (item(2, 0, (0, 0, 50, 50)),)))

    late = tracker.update(batch(session_id, 1, 250, (item(1, 0, (100, 0, 150, 50)),)))
    next_batch = tracker.update(batch(session_id, 3, 750, (item(3, 0, (5, 0, 55, 50)),)))

    assert accepted.late_result_discarded is False
    assert late.late_result_discarded is True
    assert late.observations == ()
    assert late.integrity_flags == frozenset({"LATE_RESULT_DISCARDED"})
    assert next_batch.observations[0].track_id == 1


def test_continuity_change_expires_tracks_and_ids_remain_monotonic() -> None:
    session_id = uuid4()
    tracker = LightweightTracker()
    tracker.update(batch(session_id, 1, 0, (item(1, 0, (0, 0, 50, 50)),)))

    tracker.mark_discontinuity(1)
    result = tracker.update(
        batch(session_id, 2, 250, (item(2, 0, (100, 0, 150, 50)),), continuity_segment=1)
    )

    assert result.observations[0].track_id == 1
    assert result.observations[0].state is TrackState.EXPIRED
    assert result.observations[0].ambiguous is True
    assert result.observations[1].track_id == 2
    assert "SOURCE_INTERRUPTED" in result.integrity_flags


def test_new_session_reset_restarts_track_ids_and_rejects_unexpected_session() -> None:
    first_session = uuid4()
    second_session = uuid4()
    tracker = LightweightTracker()
    tracker.update(batch(first_session, 1, 0, (item(1, 0, (0, 0, 50, 50)),)))

    with pytest.raises(ValueError, match="reset"):
        tracker.update(batch(second_session, 1, 0, (item(1, 0, (0, 0, 50, 50)),)))

    tracker.reset(second_session)
    result = tracker.update(batch(second_session, 1, 0, (item(1, 0, (0, 0, 50, 50)),)))

    assert result.observations[0].track_id == 1

from __future__ import annotations

from uuid import uuid4

import numpy as np
import pytest

from app.vision.detector import Detection, DetectionContext, FrameEnvelope
from app.vision.replay_detector import ReplayDetector


def make_frame(sequence: int = 7) -> FrameEnvelope:
    return FrameEnvelope(
        session_id=uuid4(),
        continuity_segment=0,
        sequence=sequence,
        source_timestamp_ms=1_750,
        captured_monotonic_ns=10_000,
        image_bgr=np.zeros((720, 1280, 3), dtype=np.uint8),
    )


def detection(
    detection_id: str,
    class_name: str,
    confidence: float,
    bbox: tuple[float, float, float, float],
) -> Detection:
    return Detection(
        detection_id=detection_id,
        class_id=0,
        class_name=class_name,
        confidence=confidence,
        bbox_xyxy=bbox,
    )


def test_frame_envelope_derives_dimensions_and_rejects_invalid_images() -> None:
    frame = make_frame()

    assert (frame.width, frame.height) == (1280, 720)
    with pytest.raises(ValueError, match="uint8 BGR"):
        FrameEnvelope(
            session_id=frame.session_id,
            continuity_segment=0,
            sequence=0,
            source_timestamp_ms=0,
            captured_monotonic_ns=0,
            image_bgr=np.zeros((10, 10), dtype=np.uint8),
        )


def test_detection_rejects_invalid_confidence_or_bbox() -> None:
    with pytest.raises(ValueError, match="confidence"):
        detection("1:0", "box", 1.1, (0, 0, 10, 10))
    with pytest.raises(ValueError, match="ordered"):
        detection("1:0", "box", 0.9, (10, 0, 5, 10))


def test_replay_detector_filters_classes_confidence_and_bounds() -> None:
    frame = make_frame()
    detector = ReplayDetector(
        model_id="replay-fixture-v1",
        observations_by_sequence={
            frame.sequence: (
                detection("ignored", "box", 0.99, (10, 20, 110, 220)),
                detection("ignored", "bag", 0.80, (200, 40, 300, 260)),
                detection("ignored", "person", 0.99, (400, 20, 600, 700)),
                detection("ignored", "box", 0.20, (800, 30, 900, 240)),
                detection("ignored", "box", 0.95, (1200, 30, 1400, 240)),
            )
        },
        clock_ns=iter((20_000, 24_000)).__next__,
    )

    batch = detector.detect(
        frame,
        DetectionContext(allowed_class_names=frozenset({"box", "bag"}), threshold=0.75),
    )

    assert batch.session_id == frame.session_id
    assert batch.frame_sequence == frame.sequence
    assert batch.source_timestamp_ms == frame.source_timestamp_ms
    assert batch.model_id == "replay-fixture-v1"
    assert batch.started_monotonic_ns == 20_000
    assert batch.completed_monotonic_ns == 24_000
    assert [item.detection_id for item in batch.detections] == ["7:0", "7:1"]
    assert [item.class_name for item in batch.detections] == ["box", "bag"]


def test_replay_detector_returns_three_distinct_supported_goods_in_input_order() -> None:
    frame = make_frame(sequence=8)
    goods = tuple(
        detection("fixture", "box", 0.95 - index * 0.01, (index * 50, 0, index * 50 + 40, 80))
        for index in range(3)
    )
    detector = ReplayDetector(
        model_id="replay-fixture-v1",
        observations_by_sequence={8: goods},
        clock_ns=lambda: 1,
    )

    first = detector.detect(frame, DetectionContext(frozenset({"box"}), 0.5))
    second = detector.detect(frame, DetectionContext(frozenset({"box"}), 0.5))

    assert first.detections == second.detections
    assert len({item.detection_id for item in first.detections}) == 3


def test_replay_detector_uses_empty_batch_for_sequence_without_observations() -> None:
    frame = make_frame(sequence=99)
    detector = ReplayDetector(
        model_id="replay-fixture-v1", observations_by_sequence={}, clock_ns=lambda: 1
    )

    batch = detector.detect(frame, DetectionContext(frozenset({"box"}), 0.5))

    assert batch.detections == ()


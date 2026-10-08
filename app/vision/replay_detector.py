from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import replace

from app.vision.detector import (
    ClockNs,
    Detection,
    DetectionBatch,
    DetectionContext,
    FrameEnvelope,
)


class ReplayDetector:
    """Provides deterministic fixture observations through the production detector contract."""

    def __init__(
        self,
        model_id: str,
        observations_by_sequence: Mapping[int, tuple[Detection, ...]],
        clock_ns: ClockNs = time.monotonic_ns,
    ) -> None:
        if not model_id:
            raise ValueError("model_id is required")
        if any(sequence < 0 for sequence in observations_by_sequence):
            raise ValueError("replay sequences must be non-negative")
        self._model_id = model_id
        self._observations_by_sequence = dict(observations_by_sequence)
        self._clock_ns = clock_ns

    def detect(self, frame: FrameEnvelope, context: DetectionContext) -> DetectionBatch:
        started = self._clock_ns()
        accepted: list[Detection] = []
        for fixture in self._observations_by_sequence.get(frame.sequence, ()):
            if fixture.class_name not in context.allowed_class_names:
                continue
            if fixture.confidence < context.threshold:
                continue
            _, _, right, bottom = fixture.bbox_xyxy
            if right > frame.width or bottom > frame.height:
                continue
            accepted.append(
                replace(fixture, detection_id=f"{frame.sequence}:{len(accepted)}")
            )
        completed = self._clock_ns()
        return DetectionBatch(
            session_id=frame.session_id,
            continuity_segment=frame.continuity_segment,
            frame_sequence=frame.sequence,
            source_timestamp_ms=frame.source_timestamp_ms,
            started_monotonic_ns=started,
            completed_monotonic_ns=completed,
            model_id=self._model_id,
            detections=tuple(accepted),
        )


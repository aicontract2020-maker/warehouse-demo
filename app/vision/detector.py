from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class FrameEnvelope:
    session_id: UUID
    continuity_segment: int
    sequence: int
    source_timestamp_ms: int
    captured_monotonic_ns: int
    image_bgr: NDArray[np.uint8]

    def __post_init__(self) -> None:
        if self.continuity_segment < 0 or self.sequence < 0:
            raise ValueError("continuity segment and sequence must be non-negative")
        if self.source_timestamp_ms < 0 or self.captured_monotonic_ns < 0:
            raise ValueError("frame timestamps must be non-negative")
        if (
            not isinstance(self.image_bgr, np.ndarray)
            or self.image_bgr.dtype != np.uint8
            or self.image_bgr.ndim != 3
            or self.image_bgr.shape[2] != 3
            or self.image_bgr.shape[0] <= 0
            or self.image_bgr.shape[1] <= 0
        ):
            raise ValueError("image_bgr must be a non-empty uint8 BGR array [height,width,3]")

    @property
    def width(self) -> int:
        return int(self.image_bgr.shape[1])

    @property
    def height(self) -> int:
        return int(self.image_bgr.shape[0])


@dataclass(frozen=True, slots=True)
class DetectionContext:
    allowed_class_names: frozenset[str]
    threshold: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.threshold) or not 0 <= self.threshold <= 1:
            raise ValueError("threshold must be in [0, 1]")
        if any(not class_name for class_name in self.allowed_class_names):
            raise ValueError("allowed class names cannot be empty strings")


@dataclass(frozen=True, slots=True)
class Detection:
    detection_id: str
    class_id: int
    class_name: str
    confidence: float
    bbox_xyxy: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        if not self.detection_id or not self.class_name:
            raise ValueError("detection and class names are required")
        if self.class_id < 0:
            raise ValueError("class_id must be non-negative")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")
        if len(self.bbox_xyxy) != 4 or not all(map(math.isfinite, self.bbox_xyxy)):
            raise ValueError("bbox must contain four finite coordinates")
        left, top, right, bottom = self.bbox_xyxy
        if left < 0 or top < 0 or right <= left or bottom <= top:
            raise ValueError("bbox coordinates must be non-negative and ordered")


@dataclass(frozen=True, slots=True)
class DetectionBatch:
    session_id: UUID
    continuity_segment: int
    frame_sequence: int
    source_timestamp_ms: int
    started_monotonic_ns: int
    completed_monotonic_ns: int
    model_id: str
    detections: tuple[Detection, ...]

    def __post_init__(self) -> None:
        if self.completed_monotonic_ns < self.started_monotonic_ns:
            raise ValueError("detector completion cannot precede start")
        if not self.model_id:
            raise ValueError("model_id is required")


@dataclass(frozen=True, slots=True)
class DetectionFailure(Exception):
    code: str
    message: str
    frame_sequence: int


class Detector(Protocol):
    def detect(self, frame: FrameEnvelope, context: DetectionContext) -> DetectionBatch: ...


ClockNs = Callable[[], int]


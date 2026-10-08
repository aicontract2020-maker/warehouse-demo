from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np
from numpy.typing import NDArray

SUPPORTED_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".avi", ".mkv", ".m4v"})


class SourceFailure(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ReadKind(StrEnum):
    FRAME = "frame"
    PAUSED = "paused"
    ENDED = "ended"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SourceRead:
    kind: ReadKind
    image_bgr: NDArray[np.uint8] | None = None
    source_timestamp_ms: int | None = None
    error_code: str | None = None


class VideoSource(Protocol):
    @property
    def continuity_segment(self) -> int: ...

    def open(self) -> None: ...

    def read(self) -> SourceRead: ...

    def release(self) -> None: ...


CaptureFactory = Callable[[Any], Any]


class FileVideoSource:
    def __init__(
        self,
        path: Path,
        *,
        capture_factory: CaptureFactory = cv2.VideoCapture,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._path = path.expanduser().resolve()
        self._capture_factory = capture_factory
        self._monotonic_ns = monotonic_ns
        self._sleep = sleep
        self._capture: Any | None = None
        self._paused = False
        self._last_source_timestamp_ms = -1
        self._playback_origin_ns: int | None = None

    @property
    def continuity_segment(self) -> int:
        return 0

    def open(self) -> None:
        if self._path.suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
            raise SourceFailure("UNSUPPORTED_MEDIA", self._path.suffix)
        if not self._path.is_file():
            raise SourceFailure("FILE_NOT_FOUND", str(self._path))
        capture = self._capture_factory(str(self._path))
        if not capture.isOpened():
            capture.release()
            raise SourceFailure("DECODE_FAILED", self._path.name)
        self._capture = capture

    def read(self) -> SourceRead:
        capture = self._require_capture()
        if self._paused:
            return SourceRead(ReadKind.PAUSED)
        success, image = capture.read()
        if not success or image is None:
            return SourceRead(ReadKind.ENDED)
        raw_timestamp = capture.get(cv2.CAP_PROP_POS_MSEC)
        source_timestamp_ms = max(
            self._last_source_timestamp_ms + 1, round(max(0.0, raw_timestamp))
        )
        now_ns = self._monotonic_ns()
        if self._playback_origin_ns is None:
            self._playback_origin_ns = now_ns - source_timestamp_ms * 1_000_000
        target_ns = self._playback_origin_ns + source_timestamp_ms * 1_000_000
        if target_ns > now_ns:
            self._sleep((target_ns - now_ns) / 1_000_000_000)
        self._last_source_timestamp_ms = source_timestamp_ms
        return SourceRead(ReadKind.FRAME, image, source_timestamp_ms)

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self._playback_origin_ns = None

    def release(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def _require_capture(self) -> Any:
        if self._capture is None:
            raise SourceFailure("SOURCE_NOT_OPEN", self._path.name)
        return self._capture


class CameraVideoSource:
    def __init__(
        self,
        camera_index: int,
        *,
        requested_width: int = 1280,
        requested_height: int = 720,
        capture_factory: CaptureFactory = cv2.VideoCapture,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if camera_index < 0:
            raise ValueError("camera index must be non-negative")
        self._camera_index = camera_index
        self._requested_width = requested_width
        self._requested_height = requested_height
        self._capture_factory = capture_factory
        self._monotonic_ns = monotonic_ns
        self._capture: Any | None = None
        self._started_ns: int | None = None

    @property
    def continuity_segment(self) -> int:
        return 0

    def open(self) -> None:
        capture = self._capture_factory(self._camera_index)
        if not capture.isOpened():
            capture.release()
            raise SourceFailure("CAMERA_OPEN_FAILED", str(self._camera_index))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, float(self._requested_width))
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self._requested_height))
        self._capture = capture
        self._started_ns = self._monotonic_ns()

    def read(self) -> SourceRead:
        if self._capture is None or self._started_ns is None:
            raise SourceFailure("SOURCE_NOT_OPEN", str(self._camera_index))
        success, image = self._capture.read()
        if not success or image is None:
            return SourceRead(ReadKind.ERROR, error_code="CAMERA_READ_FAILED")
        timestamp_ms = max(0, (self._monotonic_ns() - self._started_ns) // 1_000_000)
        return SourceRead(ReadKind.FRAME, image, int(timestamp_ms))

    def release(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None


def probe_cameras(
    max_indexes: int = 5,
    *,
    capture_factory: CaptureFactory = cv2.VideoCapture,
) -> tuple[dict[str, object], ...]:
    if max_indexes < 0:
        raise ValueError("max_indexes cannot be negative")
    available: list[dict[str, object]] = []
    for index in range(max_indexes):
        capture = capture_factory(index)
        try:
            if capture.isOpened():
                available.append(
                    {"index": index, "label": f"Camera {index}", "available": True}
                )
        finally:
            capture.release()
    return tuple(available)

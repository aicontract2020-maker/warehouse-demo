from __future__ import annotations

import threading
from dataclasses import dataclass

from app.vision.detector import FrameEnvelope


@dataclass(frozen=True, slots=True)
class FrameBufferMetrics:
    published_frames: int
    dropped_frames: int
    late_frames: int
    queue_depth: int
    latest_sequence: int | None


class LatestFrameBuffer:
    """Single-latest-frame handoff with overwrite/drop accounting."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._latest: FrameEnvelope | None = None
        self._last_consumed_sequence = -1
        self._published_frames = 0
        self._dropped_frames = 0
        self._late_frames = 0
        self._closed = False

    def publish(self, frame: FrameEnvelope) -> bool:
        with self._condition:
            if self._closed:
                return False
            if self._latest is not None and frame.sequence <= self._latest.sequence:
                self._late_frames += 1
                return False
            if (
                self._latest is not None
                and self._latest.sequence > self._last_consumed_sequence
            ):
                self._dropped_frames += 1
            self._latest = frame
            self._published_frames += 1
            self._condition.notify_all()
            return True

    def latest_after(self, sequence: int) -> FrameEnvelope | None:
        with self._condition:
            return self._consume_latest_after(sequence)

    def wait_after(
        self, sequence: int, timeout: float | None = None
    ) -> FrameEnvelope | None:
        with self._condition:
            self._condition.wait_for(
                lambda: self._closed
                or (self._latest is not None and self._latest.sequence > sequence),
                timeout,
            )
            if self._closed:
                return None
            return self._consume_latest_after(sequence)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @property
    def metrics(self) -> FrameBufferMetrics:
        with self._condition:
            latest_sequence = self._latest.sequence if self._latest else None
            queue_depth = int(
                self._latest is not None
                and self._latest.sequence > self._last_consumed_sequence
            )
            return FrameBufferMetrics(
                published_frames=self._published_frames,
                dropped_frames=self._dropped_frames,
                late_frames=self._late_frames,
                queue_depth=queue_depth,
                latest_sequence=latest_sequence,
            )

    def _consume_latest_after(self, sequence: int) -> FrameEnvelope | None:
        if self._latest is None or self._latest.sequence <= sequence:
            return None
        self._last_consumed_sequence = max(
            self._last_consumed_sequence, self._latest.sequence
        )
        return self._latest


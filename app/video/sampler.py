from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from app.vision.detector import FrameEnvelope


class SampleReason(StrEnum):
    SELECTED = "selected"
    NOT_DUE = "not_due"
    LATE_SEQUENCE = "late_sequence"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SampleDecision:
    selected: bool
    reason: SampleReason
    frame: FrameEnvelope | None


@dataclass(frozen=True, slots=True)
class SamplerMetrics:
    analyzed_frames: int
    skipped_not_due: int
    late_results: int
    queue_depth: int
    mean_latency_ms: float
    p95_latency_ms: float
    overloaded: bool


class FrameSampler:
    def __init__(self, target_fps: int, latency_budget_ms: float = 1_000) -> None:
        if target_fps not in {3, 4, 5}:
            raise ValueError("target FPS must be 3, 4, or 5")
        if latency_budget_ms <= 0:
            raise ValueError("latency budget must be positive")
        self._interval_ns = round(1_000_000_000 / target_fps)
        self._latency_budget_ms = latency_budget_ms
        self._next_due_ns: int | None = None
        self._last_selected_sequence = -1
        self._analyzed_frames = 0
        self._skipped_not_due = 0
        self._late_results = 0
        self._latencies_ms: list[float] = []
        self._cancelled = False

    def consider(self, frame: FrameEnvelope, now_ns: int) -> SampleDecision:
        if self._cancelled:
            return SampleDecision(False, SampleReason.CANCELLED, None)
        if frame.sequence <= self._last_selected_sequence:
            self._late_results += 1
            return SampleDecision(False, SampleReason.LATE_SEQUENCE, None)
        if self._next_due_ns is None:
            self._next_due_ns = now_ns
        if now_ns < self._next_due_ns:
            self._skipped_not_due += 1
            return SampleDecision(False, SampleReason.NOT_DUE, None)

        self._last_selected_sequence = frame.sequence
        self._analyzed_frames += 1
        while self._next_due_ns <= now_ns:
            self._next_due_ns += self._interval_ns
        return SampleDecision(True, SampleReason.SELECTED, frame)

    def record_result(self, frame: FrameEnvelope, completed_monotonic_ns: int) -> None:
        latency_ns = max(0, completed_monotonic_ns - frame.captured_monotonic_ns)
        self._latencies_ms.append(latency_ns / 1_000_000)

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def metrics(self) -> SamplerMetrics:
        mean = (
            sum(self._latencies_ms) / len(self._latencies_ms)
            if self._latencies_ms
            else 0.0
        )
        if self._latencies_ms:
            ordered = sorted(self._latencies_ms)
            p95 = ordered[math.ceil(len(ordered) * 0.95) - 1]
        else:
            p95 = 0.0
        return SamplerMetrics(
            analyzed_frames=self._analyzed_frames,
            skipped_not_due=self._skipped_not_due,
            late_results=self._late_results,
            queue_depth=0,
            mean_latency_ms=mean,
            p95_latency_ms=p95,
            overloaded=p95 > self._latency_budget_ms,
        )


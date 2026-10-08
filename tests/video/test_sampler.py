from __future__ import annotations

import threading
import time
from uuid import uuid4

import numpy as np
import pytest

from app.video.buffer import LatestFrameBuffer
from app.video.sampler import FrameSampler, SampleReason
from app.vision.detector import FrameEnvelope


def frame(sequence: int, captured_ns: int = 0) -> FrameEnvelope:
    return FrameEnvelope(
        session_id=uuid4(),
        continuity_segment=0,
        sequence=sequence,
        source_timestamp_ms=sequence * 33,
        captured_monotonic_ns=captured_ns,
        image_bgr=np.zeros((8, 8, 3), dtype=np.uint8),
    )


def test_latest_frame_buffer_replaces_stale_unconsumed_frame() -> None:
    buffer = LatestFrameBuffer()

    buffer.publish(frame(1))
    buffer.publish(frame(2))
    assert buffer.metrics.queue_depth == 1
    latest = buffer.latest_after(0)

    assert latest is not None and latest.sequence == 2
    assert buffer.metrics.published_frames == 2
    assert buffer.metrics.dropped_frames == 1
    assert buffer.metrics.queue_depth == 0


def test_reading_latest_frame_prevents_it_from_counting_as_dropped() -> None:
    buffer = LatestFrameBuffer()
    buffer.publish(frame(1))
    assert buffer.latest_after(0).sequence == 1  # type: ignore[union-attr]

    buffer.publish(frame(2))

    assert buffer.metrics.dropped_frames == 0


def test_buffer_rejects_out_of_order_publish_and_close_wakes_waiter() -> None:
    buffer = LatestFrameBuffer()
    buffer.publish(frame(2))
    assert buffer.publish(frame(1)) is False
    received: list[FrameEnvelope | None] = []
    waiter = threading.Thread(target=lambda: received.append(buffer.wait_after(2, timeout=1)))
    waiter.start()
    time.sleep(0.01)

    buffer.close()
    waiter.join(0.5)

    assert received == [None]
    assert buffer.metrics.late_frames == 1
    assert waiter.is_alive() is False


@pytest.mark.parametrize("target_fps", [3, 4, 5])
def test_sampler_holds_selected_rate_over_thirty_seconds(target_fps: int) -> None:
    sampler = FrameSampler(target_fps)
    selected = 0
    source_fps = 30
    duration_seconds = 30
    for sequence in range(source_fps * duration_seconds):
        now_ns = round(sequence / source_fps * 1_000_000_000)
        decision = sampler.consider(frame(sequence, now_ns), now_ns)
        selected += decision.selected

    measured = selected / duration_seconds
    assert measured == pytest.approx(target_fps, rel=0.15)
    assert sampler.metrics.analyzed_frames == selected


def test_sampler_drops_intermediate_frames_instead_of_building_backlog() -> None:
    sampler = FrameSampler(4)

    first = sampler.consider(frame(1, 0), 0)
    too_soon = sampler.consider(frame(2, 10_000_000), 10_000_000)
    newest_due = sampler.consider(frame(20, 250_000_000), 250_000_000)

    assert first.selected is True
    assert too_soon.reason is SampleReason.NOT_DUE
    assert newest_due.selected is True
    assert sampler.metrics.skipped_not_due == 1
    assert sampler.metrics.queue_depth == 0


def test_sampler_discards_late_sequence_without_changing_last_applied() -> None:
    sampler = FrameSampler(4)
    sampler.consider(frame(2, 0), 0)

    late = sampler.consider(frame(1, 250_000_000), 250_000_000)
    next_frame = sampler.consider(frame(3, 250_000_000), 250_000_000)

    assert late.reason is SampleReason.LATE_SEQUENCE
    assert next_frame.selected is True
    assert sampler.metrics.late_results == 1


def test_result_latency_tracks_p95_and_reports_overload() -> None:
    sampler = FrameSampler(4, latency_budget_ms=1_000)
    for sequence, latency_ms in enumerate((100, 200, 300, 400, 1_500), start=1):
        sampler.record_result(frame(sequence, 0), latency_ms * 1_000_000)

    assert sampler.metrics.mean_latency_ms == pytest.approx(500)
    assert sampler.metrics.p95_latency_ms == pytest.approx(1_500)
    assert sampler.metrics.overloaded is True


def test_sampler_validates_rate_and_supports_prompt_cancellation() -> None:
    with pytest.raises(ValueError, match="3, 4, or 5"):
        FrameSampler(10)

    sampler = FrameSampler(4)
    sampler.cancel()
    decision = sampler.consider(frame(1), 0)

    assert decision.reason is SampleReason.CANCELLED

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import cv2
import numpy as np
import pytest

from app.video.buffer import LatestFrameBuffer
from app.video.capture import CaptureWorker
from app.video.sources import (
    CameraVideoSource,
    FileVideoSource,
    ReadKind,
    SourceFailure,
    probe_cameras,
)


class FakeCapture:
    def __init__(
        self,
        frames: list[np.ndarray] | None = None,
        timestamps_ms: list[float] | None = None,
        *,
        opened: bool = True,
        fps: float = 30,
    ) -> None:
        self.frames = frames or []
        self.timestamps_ms = timestamps_ms or [
            index * 1000 / fps for index in range(len(self.frames))
        ]
        self.opened = opened
        self.fps = fps
        self.index = 0
        self.released = False
        self.set_calls: list[tuple[int, float]] = []

    def isOpened(self) -> bool:
        return self.opened

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self.index >= len(self.frames):
            return False, None
        value = self.frames[self.index]
        self.index += 1
        return True, value

    def get(self, property_id: int) -> float:
        if property_id == cv2.CAP_PROP_FPS:
            return self.fps
        if property_id == cv2.CAP_PROP_POS_MSEC:
            return self.timestamps_ms[max(0, self.index - 1)]
        if property_id == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.frames[0].shape[1]) if self.frames else 0
        if property_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.frames[0].shape[0]) if self.frames else 0
        return 0

    def set(self, property_id: int, value: float) -> bool:
        self.set_calls.append((property_id, value))
        return True

    def release(self) -> None:
        self.released = True


def image(value: int = 0) -> np.ndarray:
    return np.full((12, 16, 3), value, dtype=np.uint8)


def clock(values: list[int]) -> Callable[[], int]:
    iterator = iter(values)
    return iterator.__next__


def test_file_source_reads_monotonic_timestamps_paces_and_ends_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "pick.mp4"
    path.write_bytes(b"fixture")
    capture = FakeCapture([image(1), image(2), image(3)], [0, 100, 80])
    sleeps: list[float] = []
    source = FileVideoSource(
        path,
        capture_factory=lambda _path: capture,
        monotonic_ns=clock([1_000_000_000, 1_050_000_000, 1_100_000_000]),
        sleep=sleeps.append,
    )
    source.open()

    first = source.read()
    second = source.read()
    third = source.read()
    ended = source.read()

    assert [first.source_timestamp_ms, second.source_timestamp_ms, third.source_timestamp_ms] == [
        0,
        100,
        101,
    ]
    assert sleeps == [pytest.approx(0.05), pytest.approx(0.001)]
    assert ended.kind is ReadKind.ENDED
    source.release()
    assert capture.released is True


def test_file_pause_does_not_read_or_advance_source(tmp_path: Path) -> None:
    path = tmp_path / "pick.mov"
    path.write_bytes(b"fixture")
    capture = FakeCapture([image()], [0])
    source = FileVideoSource(path, capture_factory=lambda _path: capture)
    source.open()
    source.pause()

    paused = source.read()
    source.resume()
    resumed = source.read()

    assert paused.kind is ReadKind.PAUSED
    assert capture.index == 1
    assert resumed.kind is ReadKind.FRAME


@pytest.mark.parametrize("suffix", [".txt", ".jpg", ".exe"])
def test_file_source_rejects_unsupported_media_without_opening(
    tmp_path: Path, suffix: str
) -> None:
    path = tmp_path / f"bad{suffix}"
    path.write_bytes(b"bad")
    opened = False

    def factory(_path: str) -> FakeCapture:
        nonlocal opened
        opened = True
        return FakeCapture()

    with pytest.raises(SourceFailure) as failure:
        FileVideoSource(path, capture_factory=factory).open()

    assert failure.value.code == "UNSUPPORTED_MEDIA"
    assert opened is False


def test_file_source_reports_decode_failure(tmp_path: Path) -> None:
    path = tmp_path / "damaged.mp4"
    path.write_bytes(b"bad")

    with pytest.raises(SourceFailure) as failure:
        FileVideoSource(path, capture_factory=lambda _path: FakeCapture(opened=False)).open()

    assert failure.value.code == "DECODE_FAILED"


def test_camera_requests_resolution_and_reports_open_failure() -> None:
    capture = FakeCapture([image()])
    camera = CameraVideoSource(0, capture_factory=lambda _index: capture)
    camera.open()

    result = camera.read()

    assert result.kind is ReadKind.FRAME
    assert (cv2.CAP_PROP_FRAME_WIDTH, 1280.0) in capture.set_calls
    assert (cv2.CAP_PROP_FRAME_HEIGHT, 720.0) in capture.set_calls

    with pytest.raises(SourceFailure) as failure:
        CameraVideoSource(1, capture_factory=lambda _index: FakeCapture(opened=False)).open()
    assert failure.value.code == "CAMERA_OPEN_FAILED"


def test_camera_probe_releases_every_tested_device() -> None:
    captures = {index: FakeCapture(opened=index == 1) for index in range(3)}

    cameras = probe_cameras(3, capture_factory=lambda index: captures[index])

    assert cameras == ({"index": 1, "label": "Camera 1", "available": True},)
    assert all(capture.released for capture in captures.values())


def test_capture_worker_publishes_frames_with_session_sequence_and_releases() -> None:
    capture = FakeCapture([image(1), image(2)], [0, 33])
    camera = CameraVideoSource(
        0,
        capture_factory=lambda _index: capture,
        monotonic_ns=clock([100, 200, 300, 400]),
    )
    camera.open()
    buffer = LatestFrameBuffer()
    session_id = UUID(int=7)
    worker = CaptureWorker(camera, buffer, session_id, monotonic_ns=clock([1_000, 2_000]))

    assert worker.capture_once().kind is ReadKind.FRAME
    first = buffer.latest_after(-1)
    assert worker.capture_once().kind is ReadKind.FRAME
    second = buffer.latest_after(0)
    worker.stop()

    assert first is not None and first.session_id == session_id and first.sequence == 0
    assert second is not None and second.session_id == session_id and second.sequence == 1
    assert capture.released is True


def test_new_capture_worker_does_not_leak_previous_source_frames() -> None:
    first_capture = FakeCapture([image(1)], [0])
    second_capture = FakeCapture([image(2)], [0])
    first_source = CameraVideoSource(0, capture_factory=lambda _index: first_capture)
    second_source = CameraVideoSource(1, capture_factory=lambda _index: second_capture)
    first_source.open()
    second_source.open()
    first_buffer = LatestFrameBuffer()
    second_buffer = LatestFrameBuffer()
    first_worker = CaptureWorker(first_source, first_buffer, UUID(int=1))
    second_worker = CaptureWorker(second_source, second_buffer, UUID(int=2))

    first_worker.capture_once()
    first_worker.stop()
    second_worker.capture_once()
    second_frame = second_buffer.latest_after(-1)

    assert first_capture.released is True
    assert second_frame is not None and second_frame.session_id == UUID(int=2)
    assert second_frame.sequence == 0

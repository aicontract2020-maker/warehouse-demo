from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import cv2
import numpy as np
import pytest

from app.domain.crossing import ActionType, DirectionalAction
from app.domain.event_machine import (
    EventContext,
    EventFrame,
    EventMachine,
    EventState,
    SourceHealth,
)
from app.video.buffer import LatestFrameBuffer
from app.video.capture import CaptureWorker
from app.video.network_source import (
    RECONNECT_BACKOFF_SECONDS,
    NetworkSourceHealth,
    NetworkSourceState,
    NetworkVideoSource,
    reconnect_delay_s,
    redact_url,
    validate_network_url,
)
from app.video.sources import ReadKind, SourceFailure


def image(value: int = 0) -> np.ndarray:
    return np.full((12, 16, 3), value, dtype=np.uint8)


class FakeStream:
    """Scripted OpenCV-like capture: a list of read outcomes, then failures."""

    def __init__(self, reads: Sequence[bool] = (), *, opened: bool = True) -> None:
        self.reads = list(reads)
        self.opened = opened
        self.released = False
        self.read_calls = 0

    def isOpened(self) -> bool:
        return self.opened

    def read(self) -> tuple[bool, np.ndarray | None]:
        self.read_calls += 1
        if not self.reads:
            return False, None
        success = self.reads.pop(0)
        return (True, image(self.read_calls)) if success else (False, None)

    def release(self) -> None:
        self.released = True


class FakeNetwork:
    """Injected clock, sleep, and capture factory; nothing touches a real network."""

    def __init__(self, streams: Sequence[FakeStream]) -> None:
        self.streams = list(streams)
        self.opened: list[FakeStream] = []
        self.factory_calls: list[tuple[str, int, tuple[int, ...]]] = []
        self.now_ns = 1_000_000_000
        self.sleeps: list[float] = []

    def factory(self, url: str, api_preference: int, params: Sequence[int]) -> Any:
        self.factory_calls.append((url, api_preference, tuple(params)))
        stream = self.streams.pop(0) if self.streams else FakeStream(opened=False)
        self.opened.append(stream)
        return stream

    def monotonic_ns(self) -> int:
        return self.now_ns

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now_ns += round(seconds * 1_000_000_000)

    def advance_ms(self, milliseconds: int) -> None:
        self.now_ns += milliseconds * 1_000_000


def make_source(
    network: FakeNetwork,
    url: str = "rtsp://camera.local:554/stream",
    **kwargs: Any,
) -> NetworkVideoSource:
    kwargs.setdefault("environ", {})
    return NetworkVideoSource(
        url,
        capture_factory=network.factory,
        monotonic_ns=network.monotonic_ns,
        sleep=network.sleep,
        **kwargs,
    )


@pytest.mark.parametrize(
    "url",
    [
        "rtsp://camera.local/stream",
        "rtsps://camera.local:322/stream",
        "http://192.168.1.20:8080/video.mjpg",
        "https://camera.local/live.m3u8",
    ],
)
def test_allowed_schemes_are_accepted(url: str) -> None:
    assert validate_network_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://camera.local/stream",
        "file:///etc/passwd",
        "udp://239.0.0.1:1234",
        "rtsp:///no-host",
        "camera.local/stream",
        "",
        "http://camera.local/stream\nInjected: header",
    ],
)
def test_invalid_urls_are_rejected_before_any_capture_is_created(url: str) -> None:
    network = FakeNetwork([FakeStream([True])])

    with pytest.raises(SourceFailure) as failure:
        make_source(network, url)

    assert failure.value.code == "INVALID_SOURCE_URL"
    assert network.factory_calls == []


def test_credentials_are_redacted_from_errors_and_health() -> None:
    url = "rtsp://admin:s3cret@camera.local:554/stream"
    network = FakeNetwork([FakeStream(opened=False)])
    source = make_source(network, url)

    assert redact_url(url) == "rtsp://***@camera.local:554/stream"
    assert "s3cret" not in source.redacted_url
    with pytest.raises(SourceFailure) as failure:
        source.open()

    assert failure.value.code == "SOURCE_OPEN_FAILED"
    assert "s3cret" not in str(failure.value)
    assert "s3cret" not in repr(source.health)
    # The real URL is still used for the capture itself, only in memory.
    assert network.factory_calls[0][0] == url


def test_open_passes_finite_open_and_read_timeouts_to_ffmpeg_backend() -> None:
    network = FakeNetwork([FakeStream([True])])
    source = make_source(network, open_timeout_s=3.5, read_timeout_s=2.25)

    source.open()

    url, api_preference, params = network.factory_calls[0]
    assert url == "rtsp://camera.local:554/stream"
    assert api_preference == cv2.CAP_FFMPEG
    options = dict(zip(params[::2], params[1::2], strict=True))
    assert options == {
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC: 3_500,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC: 2_250,
    }


@pytest.mark.parametrize("bad_timeout", [0, -1, math.inf, math.nan])
def test_non_finite_or_non_positive_timeouts_are_rejected(bad_timeout: float) -> None:
    network = FakeNetwork([])

    with pytest.raises(ValueError, match="timeout"):
        make_source(network, open_timeout_s=bad_timeout)
    with pytest.raises(ValueError, match="timeout"):
        make_source(network, read_timeout_s=bad_timeout)


def test_rtsp_prefers_tcp_transport_without_overriding_operator_options() -> None:
    environ: dict[str, str] = {}
    make_source(FakeNetwork([]), environ=environ)
    assert environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == "rtsp_transport;tcp"

    custom = {"OPENCV_FFMPEG_CAPTURE_OPTIONS": "rtsp_transport;udp"}
    make_source(FakeNetwork([]), environ=custom)
    assert custom["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == "rtsp_transport;udp"

    http_environ: dict[str, str] = {}
    make_source(FakeNetwork([]), "http://camera.local/video.mjpg", environ=http_environ)
    assert "OPENCV_FFMPEG_CAPTURE_OPTIONS" not in http_environ


def test_initial_open_failure_is_typed_and_does_not_start_reconnecting() -> None:
    stream = FakeStream(opened=False)
    network = FakeNetwork([stream])
    source = make_source(network)

    with pytest.raises(SourceFailure) as failure:
        source.open()

    assert failure.value.code == "SOURCE_OPEN_FAILED"
    assert stream.released is True
    assert source.health.state is NetworkSourceState.FAILED
    assert network.sleeps == []


def test_backoff_schedule_is_bounded_at_ten_seconds() -> None:
    assert RECONNECT_BACKOFF_SECONDS == (1.0, 2.0, 4.0, 8.0, 10.0)
    assert [reconnect_delay_s(attempt) for attempt in range(1, 9)] == [
        1.0,
        2.0,
        4.0,
        8.0,
        10.0,
        10.0,
        10.0,
        10.0,
    ]
    with pytest.raises(ValueError):
        reconnect_delay_s(0)


def test_connected_reads_have_monotonic_source_timestamps_and_health() -> None:
    network = FakeNetwork([FakeStream([True, True])])
    source = make_source(network)
    source.open()

    first = source.read()
    network.advance_ms(250)
    second = source.read()

    assert first.kind is ReadKind.FRAME and second.kind is ReadKind.FRAME
    assert first.source_timestamp_ms == 0
    assert second.source_timestamp_ms == 250
    health = source.health
    assert health.state is NetworkSourceState.CONNECTED
    assert health.continuity_segment == 0
    assert health.reconnect_attempts == 0
    assert health.last_frame_monotonic_ns == network.now_ns
    assert health.event_health is SourceHealth.CONNECTED


def test_disconnect_reconnects_with_1_2_4_8_10_second_backoff_and_reports_attempts() -> None:
    first = FakeStream([True])
    failures = [FakeStream(opened=False) for _ in range(6)]
    recovered = FakeStream([True])
    network = FakeNetwork([first, *failures, recovered])
    updates: list[NetworkSourceHealth] = []
    source = make_source(network, on_health=updates.append)
    source.open()
    assert source.read().kind is ReadKind.FRAME

    disconnected = source.read()

    assert disconnected.kind is ReadKind.ERROR
    assert disconnected.error_code == "SOURCE_DISCONNECTED"
    assert first.released is True
    assert source.health.state is NetworkSourceState.RECONNECTING
    assert source.health.event_health is SourceHealth.RECONNECTING
    assert source.health.next_retry_delay_s == 1.0

    attempts: list[int] = []
    for _ in range(6):
        result = source.read()
        assert result.kind is ReadKind.ERROR
        assert result.error_code == "SOURCE_RECONNECTING"
        attempts.append(source.health.reconnect_attempts)

    assert network.sleeps == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
    assert attempts == [1, 2, 3, 4, 5, 6]
    assert all(stream.released for stream in failures)

    frame = source.read()

    assert frame.kind is ReadKind.FRAME
    assert network.sleeps[-1] == 10.0
    health = source.health
    assert health.state is NetworkSourceState.CONNECTED
    assert health.continuity_segment == 1
    assert health.reconnect_attempts == 0
    assert health.next_retry_delay_s is None
    states = [update.state for update in updates]
    assert states[0] is NetworkSourceState.OPENING
    assert NetworkSourceState.RECONNECTING in states
    assert states[-1] is NetworkSourceState.CONNECTED
    assert [update.reconnect_attempts for update in updates if update.reconnect_attempts] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]


def test_backoff_resets_after_successful_reconnect() -> None:
    network = FakeNetwork(
        [
            FakeStream([True]),
            FakeStream(opened=False),
            FakeStream([True]),
            FakeStream(opened=False),
            FakeStream([True]),
        ]
    )
    source = make_source(network)
    source.open()
    source.read()
    source.read()  # disconnect
    source.read()  # attempt 1 fails after 1 s
    assert source.read().kind is ReadKind.FRAME  # attempt 2 succeeds after 2 s
    source.read()  # disconnect again
    source.read()  # attempt 1 fails after 1 s, not 4 s
    assert source.read().kind is ReadKind.FRAME

    assert network.sleeps == [1.0, 2.0, 1.0, 2.0]
    assert source.health.continuity_segment == 2


def test_reconnect_waits_only_for_the_remaining_backoff() -> None:
    network = FakeNetwork([FakeStream([True]), FakeStream([True])])
    source = make_source(network)
    source.open()
    source.read()
    source.read()  # disconnect, retry due in 1 s
    network.advance_ms(600)

    assert source.read().kind is ReadKind.FRAME
    assert network.sleeps == [pytest.approx(0.4)]


def test_frames_after_reconnect_start_a_new_continuity_segment_with_later_timestamps() -> None:
    network = FakeNetwork([FakeStream([True]), FakeStream([True])])
    source = make_source(network)
    source.open()
    buffer = LatestFrameBuffer()
    worker = CaptureWorker(source, buffer, UUID(int=3), monotonic_ns=network.monotonic_ns)

    worker.capture_once()
    before = buffer.latest_after(-1)
    worker.capture_once()  # disconnect, nothing published
    assert buffer.latest_after(0) is None
    worker.capture_once()  # reconnect and read
    after = buffer.latest_after(0)

    assert before is not None and after is not None
    assert before.continuity_segment == 0
    assert after.continuity_segment == 1
    assert after.sequence == before.sequence + 1
    assert after.source_timestamp_ms > before.source_timestamp_ms


def test_disconnect_during_active_event_freezes_it_for_review_after_reconnect() -> None:
    network = FakeNetwork([FakeStream([True]), FakeStream([True, True])])
    source = make_source(network)
    source.open()
    machine = EventMachine()
    session_id = UUID(int=9)
    event_id = UUID(int=10)
    event_context = EventContext(
        task_id="PICK-1",
        zone_profile_id="zone-1",
        zone_version=1,
        sku_id="RICE-25KG-A",
        unit="bag",
        source_location_id="A-03-01",
        destination_location_id="STAGING",
        source_session_id=session_id,
        configuration_version="cfg-1",
    )
    pick = DirectionalAction(
        action_id=UUID(int=11),
        event_id=event_id,
        action_sequence=1,
        track_id=1,
        transition_index=1,
        action_type=ActionType.PICK,
        delta=1,
        source_timestamp_ms=0,
        frame_sequence=0,
        confidence=0.999,
        bbox_xyxy=(0, 0, 4, 4),
    )

    def feed(sequence: int, timestamp_ms: int, actions: tuple[DirectionalAction, ...] = ()):
        return machine.update(
            EventFrame(
                frame_sequence=sequence,
                source_timestamp_ms=timestamp_ms,
                motion_stable=True,
                source_health=source.health.event_health,
                unresolved_boundary_track_ids=frozenset(),
                actions=actions,
                integrity_flags=frozenset(),
            ),
            event_context,
        )

    first = source.read()
    assert first.source_timestamp_ms is not None
    assert feed(0, first.source_timestamp_ms, (pick,)).state is EventState.ACTIVE

    network.advance_ms(200)
    assert source.read().kind is ReadKind.ERROR
    interrupted = feed(1, 200)
    assert interrupted.state is EventState.REVIEW_REQUIRED
    assert interrupted.review_reason == "SOURCE_INTERRUPTED"
    assert "SOURCE_INTERRUPTED" in interrupted.integrity_flags

    reconnected = source.read()
    assert reconnected.kind is ReadKind.FRAME
    assert source.health.event_health is SourceHealth.CONNECTED
    assert reconnected.source_timestamp_ms is not None
    after = feed(2, reconnected.source_timestamp_ms)
    assert after.state is EventState.REVIEW_REQUIRED
    assert "SOURCE_INTERRUPTED" in after.integrity_flags


def test_reconnect_disabled_moves_to_failed_without_retrying() -> None:
    network = FakeNetwork([FakeStream([True]), FakeStream([True])])
    source = make_source(network, reconnect_enabled=False)
    source.open()
    source.read()

    lost = source.read()
    again = source.read()

    assert lost.kind is ReadKind.ERROR and lost.error_code == "SOURCE_DISCONNECTED"
    assert again.kind is ReadKind.ERROR and again.error_code == "SOURCE_DISCONNECTED"
    assert source.health.state is NetworkSourceState.FAILED
    assert source.health.event_health is SourceHealth.FAILED
    assert len(network.factory_calls) == 1
    assert network.sleeps == []


def test_capture_factory_exception_counts_as_failed_attempt() -> None:
    network = FakeNetwork([FakeStream([True])])
    calls = 0

    def flaky_factory(url: str, api_preference: int, params: Sequence[int]) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise cv2.error("backend exploded")
        return network.factory(url, api_preference, params)

    network.streams.append(FakeStream([True]))
    source = NetworkVideoSource(
        "http://camera.local/video.mjpg",
        capture_factory=flaky_factory,
        monotonic_ns=network.monotonic_ns,
        sleep=network.sleep,
        environ={},
    )
    source.open()
    source.read()
    source.read()  # disconnect

    failed = source.read()
    recovered = source.read()

    assert failed.error_code == "SOURCE_RECONNECTING"
    assert recovered.kind is ReadKind.FRAME
    assert network.sleeps == [1.0, 2.0]


def test_release_closes_capture_and_is_idempotent() -> None:
    stream = FakeStream([True, True])
    network = FakeNetwork([stream])
    source = make_source(network)
    source.open()
    source.read()

    source.release()
    source.release()

    assert stream.released is True
    assert source.health.state is NetworkSourceState.CLOSED
    with pytest.raises(SourceFailure) as failure:
        source.read()
    assert failure.value.code == "SOURCE_NOT_OPEN"


def test_release_during_backoff_wait_stops_without_reopening() -> None:
    network = FakeNetwork([FakeStream([True]), FakeStream([True])])
    source = make_source(network)

    def releasing_sleep(seconds: float) -> None:
        network.sleep(seconds)
        source.release()

    source._sleep = releasing_sleep  # simulate shutdown from another thread mid-wait
    source.open()
    source.read()
    source.read()  # disconnect

    result = source.read()

    assert result.kind is ReadKind.ERROR
    assert result.error_code == "SOURCE_CLOSED"
    assert len(network.factory_calls) == 1
    assert source.health.state is NetworkSourceState.CLOSED


def test_default_backoff_wait_is_interrupted_promptly_by_release() -> None:
    import threading
    import time

    first = FakeStream([True])
    calls: list[str] = []

    def factory(url: str, api_preference: int, params: Sequence[int]) -> Any:
        calls.append(url)
        return first

    source = NetworkVideoSource(
        "http://camera.local/video.mjpg", capture_factory=factory, environ={}
    )
    source.open()
    source.read()
    source.read()  # disconnect; next attempt due in 1 s of real time
    result: list[Any] = []
    reader = threading.Thread(target=lambda: result.append(source.read()))
    started = time.monotonic()
    reader.start()
    time.sleep(0.05)
    source.release()
    reader.join(timeout=2)

    assert not reader.is_alive()
    assert time.monotonic() - started < 0.9
    assert result[0].error_code == "SOURCE_CLOSED"
    assert calls == ["http://camera.local/video.mjpg"]


def test_capture_worker_stop_releases_network_source() -> None:
    stream = FakeStream([True])
    network = FakeNetwork([stream])
    source = make_source(network)
    source.open()
    worker = CaptureWorker(source, LatestFrameBuffer(), UUID(int=1))

    worker.capture_once()
    worker.stop()

    assert stream.released is True
    assert source.health.state is NetworkSourceState.CLOSED


def test_release_while_open_is_connecting_discards_the_late_capture() -> None:
    stream = FakeStream([True])
    holder: list[NetworkVideoSource] = []

    def factory(url: str, api_preference: int, params: Sequence[int]) -> Any:
        holder[0].release()  # shutdown lands while the backend is still connecting
        return stream

    source = NetworkVideoSource(
        "http://camera.local/video.mjpg", capture_factory=factory, environ={}
    )
    holder.append(source)

    with pytest.raises(SourceFailure) as failure:
        source.open()

    assert failure.value.code == "SOURCE_NOT_OPEN"
    assert stream.released is True
    assert source.health.state is NetworkSourceState.CLOSED

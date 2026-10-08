"""RTSP/HTTP network-stream source with finite timeouts and bounded reconnection."""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import cv2

from app.domain.event_machine import SourceHealth
from app.video.sources import ReadKind, SourceFailure, SourceRead

ALLOWED_NETWORK_SCHEMES = frozenset({"rtsp", "rtsps", "http", "https"})
RECONNECT_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 10.0)
FFMPEG_CAPTURE_OPTIONS_ENV = "OPENCV_FFMPEG_CAPTURE_OPTIONS"
RTSP_TCP_CAPTURE_OPTIONS = "rtsp_transport;tcp"

NetworkCaptureFactory = Callable[[str, int, Sequence[int]], Any]


class NetworkSourceState(StrEnum):
    """Mirrors `source.state` in contracts/source-monitoring-api.md."""

    CLOSED = "closed"
    OPENING = "opening"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ENDED = "ended"
    FAILED = "failed"


_EVENT_HEALTH = {
    NetworkSourceState.CLOSED: SourceHealth.ENDED,
    NetworkSourceState.OPENING: SourceHealth.RECONNECTING,
    NetworkSourceState.CONNECTED: SourceHealth.CONNECTED,
    NetworkSourceState.RECONNECTING: SourceHealth.RECONNECTING,
    NetworkSourceState.ENDED: SourceHealth.ENDED,
    NetworkSourceState.FAILED: SourceHealth.FAILED,
}


@dataclass(frozen=True, slots=True)
class NetworkSourceHealth:
    """Credential-free health snapshot published on every state transition."""

    state: NetworkSourceState
    continuity_segment: int
    reconnect_attempts: int
    next_retry_delay_s: float | None
    last_frame_monotonic_ns: int | None
    error_code: str | None

    @property
    def event_health(self) -> SourceHealth:
        return _EVENT_HEALTH[self.state]


def validate_network_url(url: str) -> str:
    """Accept only rtsp/rtsps/http/https URLs with a host; never echo credentials."""

    if not url or any(character.isspace() or ord(character) < 32 for character in url):
        raise SourceFailure("INVALID_SOURCE_URL", "URL is empty or contains control characters")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        _ = parts.port
    except ValueError as error:
        raise SourceFailure("INVALID_SOURCE_URL", "URL cannot be parsed") from error
    if parts.scheme.lower() not in ALLOWED_NETWORK_SCHEMES:
        raise SourceFailure(
            "INVALID_SOURCE_URL",
            f"scheme must be one of {', '.join(sorted(ALLOWED_NETWORK_SCHEMES))}",
        )
    if not hostname:
        raise SourceFailure("INVALID_SOURCE_URL", "URL host is required")
    return url


def redact_url(url: str) -> str:
    """Remove user-info and query values so the URL is safe for logs and responses."""

    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    query = "***" if parts.query else ""
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def reconnect_delay_s(attempt: int) -> float:
    """Delay before the 1-based reconnect attempt: 1, 2, 4, 8, then 10 seconds capped."""

    if attempt < 1:
        raise ValueError("reconnect attempts are 1-based")
    return RECONNECT_BACKOFF_SECONDS[min(attempt, len(RECONNECT_BACKOFF_SECONDS)) - 1]


def _timeout_ms(name: str, seconds: float) -> int:
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} timeout must be a finite positive number of seconds")
    return max(1, round(seconds * 1_000))


class NetworkVideoSource:
    """`VideoSource` for RTSP/HTTP streams.

    `read()` never raises for a lost stream: it returns `ReadKind.ERROR`, publishes a
    `reconnecting` health update, and the next `read()` waits out the remaining backoff
    (interruptible by `release()`) before one reopen attempt. A successful reopen starts a
    new continuity segment. Source timestamps are elapsed monotonic time since `open()`, so
    they keep increasing across reconnects.
    """

    def __init__(
        self,
        url: str,
        *,
        open_timeout_s: float = 5.0,
        read_timeout_s: float = 5.0,
        reconnect_enabled: bool = True,
        capture_factory: NetworkCaptureFactory = cv2.VideoCapture,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], object] | None = None,
        environ: MutableMapping[str, str] | None = None,
        on_health: Callable[[NetworkSourceHealth], None] | None = None,
    ) -> None:
        self._url = validate_network_url(url)
        self._redacted_url = redact_url(url)
        self._open_timeout_ms = _timeout_ms("open", open_timeout_s)
        self._read_timeout_ms = _timeout_ms("read", read_timeout_s)
        self._reconnect_enabled = reconnect_enabled
        self._capture_factory = capture_factory
        self._monotonic_ns = monotonic_ns
        self._stop_event = threading.Event()
        self._sleep: Callable[[float], object] = sleep or self._stop_event.wait
        self._on_health = on_health
        self._state_lock = threading.RLock()
        self._io_lock = threading.RLock()

        self._capture: Any | None = None
        self._released = False
        self._state = NetworkSourceState.CLOSED
        self._continuity_segment = 0
        self._reconnect_attempts = 0
        self._next_retry_delay_s: float | None = None
        self._next_attempt_ns: int | None = None
        self._last_frame_monotonic_ns: int | None = None
        self._error_code: str | None = None
        self._origin_ns: int | None = None
        self._last_source_timestamp_ms = -1

        if urlsplit(self._url).scheme.lower() in {"rtsp", "rtsps"}:
            target = os.environ if environ is None else environ
            target.setdefault(FFMPEG_CAPTURE_OPTIONS_ENV, RTSP_TCP_CAPTURE_OPTIONS)

    @property
    def redacted_url(self) -> str:
        return self._redacted_url

    @property
    def continuity_segment(self) -> int:
        with self._state_lock:
            return self._continuity_segment

    @property
    def health(self) -> NetworkSourceHealth:
        with self._state_lock:
            return self._health_locked()

    def open(self) -> None:
        with self._state_lock:
            if self._released:
                raise SourceFailure("SOURCE_NOT_OPEN", self._redacted_url)
            if self._capture is not None:
                raise SourceFailure("SOURCE_ALREADY_OPEN", self._redacted_url)
            self._set_state(NetworkSourceState.OPENING, error_code=None)
        capture = self._try_open()
        # Lock order everywhere: _io_lock before _state_lock.
        with self._io_lock, self._state_lock:
            if self._released:
                if capture is not None:
                    capture.release()
                raise SourceFailure("SOURCE_NOT_OPEN", self._redacted_url)
            if capture is None:
                self._set_state(NetworkSourceState.FAILED, error_code="SOURCE_OPEN_FAILED")
                raise SourceFailure("SOURCE_OPEN_FAILED", self._redacted_url)
            self._capture = capture
            self._origin_ns = self._monotonic_ns()
            self._set_state(NetworkSourceState.CONNECTED, error_code=None)

    def read(self) -> SourceRead:
        with self._state_lock:
            state = self._state
            if self._released or self._origin_ns is None:
                raise SourceFailure("SOURCE_NOT_OPEN", self._redacted_url)
        if state is NetworkSourceState.CONNECTED:
            return self._read_connected()
        if state is NetworkSourceState.RECONNECTING:
            return self._attempt_reconnect()
        return SourceRead(ReadKind.ERROR, error_code=self._error_code or "SOURCE_FAILED")

    def release(self) -> None:
        with self._state_lock:
            self._released = True
            self._stop_event.set()
        # Reads and opens are bounded by finite backend timeouts, so this wait is bounded.
        with self._io_lock:
            capture, self._capture = self._capture, None
            if capture is not None:
                capture.release()
        with self._state_lock:
            if self._state is not NetworkSourceState.CLOSED:
                self._next_retry_delay_s = None
                self._next_attempt_ns = None
                self._set_state(NetworkSourceState.CLOSED, error_code=None)

    def _read_connected(self) -> SourceRead:
        with self._io_lock:
            capture = self._capture
            if capture is None:
                return SourceRead(ReadKind.ERROR, error_code="SOURCE_CLOSED")
            try:
                success, image = capture.read()
            except cv2.error:
                success, image = False, None
            if success and image is not None:
                now_ns = self._monotonic_ns()
                with self._state_lock:
                    assert self._origin_ns is not None
                    elapsed_ms = max(0, (now_ns - self._origin_ns) // 1_000_000)
                    timestamp_ms = max(self._last_source_timestamp_ms + 1, int(elapsed_ms))
                    self._last_source_timestamp_ms = timestamp_ms
                    self._last_frame_monotonic_ns = now_ns
                return SourceRead(ReadKind.FRAME, image, timestamp_ms)
            self._capture = None
            capture.release()
        with self._state_lock:
            if self._released:
                return SourceRead(ReadKind.ERROR, error_code="SOURCE_CLOSED")
            if not self._reconnect_enabled:
                self._set_state(NetworkSourceState.FAILED, error_code="SOURCE_DISCONNECTED")
            else:
                self._reconnect_attempts = 0
                self._schedule_retry(reconnect_delay_s(1))
                self._set_state(NetworkSourceState.RECONNECTING, error_code="SOURCE_DISCONNECTED")
        return SourceRead(ReadKind.ERROR, error_code="SOURCE_DISCONNECTED")

    def _attempt_reconnect(self) -> SourceRead:
        with self._state_lock:
            assert self._next_attempt_ns is not None
            remaining_ns = self._next_attempt_ns - self._monotonic_ns()
        if remaining_ns > 0:
            self._sleep(remaining_ns / 1_000_000_000)
        with self._state_lock:
            if self._released:
                return SourceRead(ReadKind.ERROR, error_code="SOURCE_CLOSED")
            self._reconnect_attempts += 1
            attempt = self._reconnect_attempts
        capture = self._try_open()
        with self._io_lock, self._state_lock:
            if self._released:
                if capture is not None:
                    capture.release()
                return SourceRead(ReadKind.ERROR, error_code="SOURCE_CLOSED")
            if capture is None:
                self._schedule_retry(reconnect_delay_s(attempt + 1))
                self._set_state(NetworkSourceState.RECONNECTING, error_code="SOURCE_RECONNECTING")
                return SourceRead(ReadKind.ERROR, error_code="SOURCE_RECONNECTING")
            self._capture = capture
            self._continuity_segment += 1
            self._reconnect_attempts = 0
            self._next_retry_delay_s = None
            self._next_attempt_ns = None
            self._set_state(NetworkSourceState.CONNECTED, error_code=None)
        return self._read_connected()

    def _try_open(self) -> Any | None:
        params = (
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
            self._open_timeout_ms,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC,
            self._read_timeout_ms,
        )
        with self._io_lock:
            try:
                capture = self._capture_factory(self._url, cv2.CAP_FFMPEG, list(params))
            except cv2.error:
                return None
            try:
                opened = bool(capture.isOpened())
            except cv2.error:
                opened = False
            if not opened:
                capture.release()
                return None
            return capture

    def _schedule_retry(self, delay_s: float) -> None:
        self._next_retry_delay_s = delay_s
        self._next_attempt_ns = self._monotonic_ns() + round(delay_s * 1_000_000_000)

    def _set_state(self, state: NetworkSourceState, *, error_code: str | None) -> None:
        self._state = state
        self._error_code = error_code
        if self._on_health is not None:
            self._on_health(self._health_locked())

    def _health_locked(self) -> NetworkSourceHealth:
        return NetworkSourceHealth(
            state=self._state,
            continuity_segment=self._continuity_segment,
            reconnect_attempts=self._reconnect_attempts,
            next_retry_delay_s=self._next_retry_delay_s,
            last_frame_monotonic_ns=self._last_frame_monotonic_ns,
            error_code=self._error_code,
        )

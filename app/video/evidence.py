"""Bounded evidence ring and MP4 clip finalization (plan sections 4.11 and 6).

Frames are JPEG-encoded into a ring bounded by time, frame count, and bytes. An event pins
the ring from `first_action - pre_roll`; after completion the clip is finalized once the
ring holds `ended + post_roll`. Clips are encoded by the installed FFmpeg executable, invoked
with an argv list (never a shell string) and a finite timeout, written to a `.partial` file,
and atomically renamed. Any coverage gap or encoder problem yields an `unavailable` outcome
whose integrity flag forces review (AC-27).
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import UUID

import cv2
import numpy as np
from numpy.typing import NDArray

from app.vision.detector import FrameEnvelope

EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
ALLOWED_VIDEO_CODECS = frozenset({"libx264", "mpeg4"})
FFMPEG_EXECUTABLE = "ffmpeg"

Runner = Callable[..., Any]
JpegEncoder = Callable[[NDArray[np.uint8]], bytes | None]


class EvidencePathError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class EvidencePolicy:
    pre_roll_ms: int = 2_000
    post_roll_ms: int = 2_000
    retention_margin_ms: int = 2_000
    capture_fps: int = 10
    max_ring_frames: int = 600
    max_ring_bytes: int = 64 * 1024 * 1024
    jpeg_quality: int = 80
    finalize_timeout_s: float = 15.0
    video_codec: str = "libx264"

    def __post_init__(self) -> None:
        if self.pre_roll_ms < 2_000 or self.post_roll_ms < 2_000:
            raise ValueError("pre/post roll must cover at least 2 seconds (AC-27)")
        if self.retention_margin_ms < 0:
            raise ValueError("retention margin cannot be negative")
        if not 1 <= self.capture_fps <= 30:
            raise ValueError("evidence capture FPS must be 1-30")
        if self.max_ring_frames <= 0 or self.max_ring_bytes <= 0:
            raise ValueError("ring bounds must be positive")
        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("JPEG quality must be 1-100")
        if not math.isfinite(self.finalize_timeout_s) or self.finalize_timeout_s <= 0:
            raise ValueError("finalize timeout must be finite and positive")
        if self.video_codec not in ALLOWED_VIDEO_CODECS:
            raise ValueError(f"video codec must be one of {sorted(ALLOWED_VIDEO_CODECS)}")


class EvidenceStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class EvidenceOutcome:
    event_id: UUID
    status: EvidenceStatus
    relative_path: str | None
    start_source_ms: int | None
    end_source_ms: int | None
    frame_count: int
    reason: str | None

    @property
    def evidence_path(self) -> str | None:
        """Value for `Events.evidence_path` and reconciliation's `evidence_path`."""

        return self.relative_path

    @property
    def integrity_flags(self) -> frozenset[str]:
        if self.status is EvidenceStatus.UNAVAILABLE:
            return frozenset({EVIDENCE_UNAVAILABLE})
        return frozenset()


@dataclass(frozen=True, slots=True)
class EvidenceMetrics:
    ring_frames: int
    ring_bytes: int
    oldest_source_ms: int | None
    newest_source_ms: int | None
    evicted_frames: int
    skipped_frames: int
    pending_events: int


@dataclass(frozen=True, slots=True)
class _EncodedFrame:
    sequence: int
    source_timestamp_ms: int
    data: bytes


@dataclass(slots=True)
class _Request:
    event_id: UUID
    window_start_ms: int
    window_end_ms: int | None = None
    failure: str | None = None


def build_ffmpeg_argv(
    ffmpeg_path: str,
    *,
    frame_count: int,
    duration_ms: int,
    codec: str,
    output: Path,
) -> list[str]:
    """Fixed argument template; only validated integers, an allow-listed codec, and a
    manager-chosen output path vary. Frames arrive as concatenated JPEGs on stdin."""

    if codec not in ALLOWED_VIDEO_CODECS:
        raise ValueError(f"codec {codec!r} is not allowed")
    if frame_count < 1:
        raise ValueError("at least one frame is required")
    if frame_count == 1 or duration_ms <= 0:
        rate = Fraction(1)
    else:
        rate = Fraction((frame_count - 1) * 1_000, duration_ms).limit_denominator(1_001)
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "image2pipe",
        "-framerate",
        f"{rate.numerator}/{rate.denominator}",
        "-c:v",
        "mjpeg",
        "-i",
        "pipe:0",
        "-an",
        "-vf",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:v",
        codec,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output),
    ]


def resolve_evidence_path(
    evidence_dir: Path, relative_path: str, *, relative_root: Path | None = None
) -> Path:
    """Resolve a stored evidence path; refuse anything outside `evidence_dir`."""

    if (
        not relative_path
        or PurePosixPath(relative_path).is_absolute()
        or PureWindowsPath(relative_path).is_absolute()
    ):
        raise EvidencePathError("PATH_NOT_ALLOWED", "evidence path must be relative")
    evidence_root = evidence_dir.expanduser().resolve()
    root = (relative_root or evidence_dir.parent).expanduser().resolve()
    candidate = (root / relative_path).resolve()
    if candidate == evidence_root or not candidate.is_relative_to(evidence_root):
        raise EvidencePathError("PATH_NOT_ALLOWED", "evidence path escapes evidence directory")
    return candidate


class EvidenceManager:
    """Per-session evidence recorder. Thread-safe; encoding runs outside the ring lock."""

    def __init__(
        self,
        evidence_dir: Path,
        session_id: UUID,
        policy: EvidencePolicy | None = None,
        *,
        relative_root: Path | None = None,
        runner: Runner = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        encode_jpeg: JpegEncoder | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._evidence_dir = evidence_dir.expanduser().resolve()
        self._relative_root = (relative_root or evidence_dir.parent).expanduser().resolve()
        if not self._evidence_dir.is_relative_to(self._relative_root):
            raise ValueError("evidence_dir must be inside relative_root")
        self._session_id = session_id
        self._policy = policy or EvidencePolicy()
        self._runner = runner
        self._which = which
        self._encode_jpeg = encode_jpeg or self._default_encode
        self._monotonic = monotonic
        self._lock = threading.Lock()

        self._ring: deque[_EncodedFrame] = deque()
        self._ring_bytes = 0
        self._segment: int | None = None
        self._segment_head_ts: int | None = None
        self._segment_head_evicted = False
        self._last_slot: int | None = None
        self._newest_ts: int | None = None
        self._evicted_frames = 0
        self._skipped_frames = 0
        self._source_ended = False
        self._closed = False
        self._requests: dict[UUID, _Request] = {}

    @property
    def metrics(self) -> EvidenceMetrics:
        with self._lock:
            return EvidenceMetrics(
                ring_frames=len(self._ring),
                ring_bytes=self._ring_bytes,
                oldest_source_ms=self._ring[0].source_timestamp_ms if self._ring else None,
                newest_source_ms=self._ring[-1].source_timestamp_ms if self._ring else None,
                evicted_frames=self._evicted_frames,
                skipped_frames=self._skipped_frames,
                pending_events=len(self._requests),
            )

    # ------------------------------------------------------------------ frames

    def add_frame(self, frame: FrameEnvelope) -> bool:
        timestamp_ms = frame.source_timestamp_ms
        with self._lock:
            if self._closed:
                return False
            if self._segment != frame.continuity_segment:
                self._start_segment(frame.continuity_segment, timestamp_ms)
            elif self._newest_ts is not None and timestamp_ms <= self._newest_ts:
                self._skipped_frames += 1
                return False
            slot = timestamp_ms * self._policy.capture_fps // 1_000
            if slot == self._last_slot:
                self._skipped_frames += 1
                return False
            self._last_slot = slot
            self._newest_ts = timestamp_ms
        data = self._encode_jpeg(frame.image_bgr)
        with self._lock:
            if self._closed or data is None:
                self._skipped_frames += int(data is None)
                return False
            self._ring.append(_EncodedFrame(frame.sequence, timestamp_ms, data))
            self._ring_bytes += len(data)
            self._evict()
        return True

    def mark_source_ended(self) -> None:
        with self._lock:
            self._source_ended = True

    # ------------------------------------------------------------------ events

    def begin_event(self, event_id: UUID, first_action_source_ms: int) -> None:
        with self._lock:
            if event_id in self._requests:
                return
            request = _Request(event_id, first_action_source_ms - self._policy.pre_roll_ms)
            if self._closed:
                request.failure = "SHUTDOWN"
            self._requests[event_id] = request

    def complete_event(self, event_id: UUID, ended_source_ms: int) -> None:
        with self._lock:
            request = self._requests[event_id]
            request.window_end_ms = ended_source_ms + self._policy.post_roll_ms

    def abandon_event(self, event_id: UUID, reason: str) -> EvidenceOutcome:
        with self._lock:
            request = self._requests.pop(event_id)
        return self._unavailable(request.event_id, reason)

    def finalize_ready(self) -> tuple[EvidenceOutcome, ...]:
        jobs: list[tuple[_Request, list[_EncodedFrame] | None, str | None]] = []
        with self._lock:
            for request in list(self._requests.values()):
                if request.window_end_ms is None:
                    continue
                frames, failure, ready = self._select(request)
                if failure is not None or ready:
                    del self._requests[request.event_id]
                    jobs.append((request, frames, failure))
            closed = self._closed
        outcomes: list[EvidenceOutcome] = []
        for request, frames, failure in jobs:
            if failure is not None or frames is None:
                outcomes.append(self._unavailable(request.event_id, failure or "NO_FRAMES"))
            elif closed:
                outcomes.append(self._unavailable(request.event_id, "SHUTDOWN"))
            else:
                outcomes.append(
                    self._encode(request.event_id, frames, self._policy.finalize_timeout_s)
                )
        return tuple(outcomes)

    def shutdown(self, timeout_s: float | None = None) -> tuple[EvidenceOutcome, ...]:
        """Stop accepting frames, finalize clips whose post-roll is already captured, and
        report every other pending event as unavailable. Encoding honours the deadline."""

        deadline = None if timeout_s is None else self._monotonic() + timeout_s
        jobs: list[tuple[_Request, list[_EncodedFrame] | None, str | None]] = []
        with self._lock:
            if self._closed:
                return ()
            self._closed = True
            for request in self._requests.values():
                if request.window_end_ms is None:
                    jobs.append((request, None, request.failure or "SHUTDOWN"))
                    continue
                frames, failure, ready = self._select(request)
                if failure is None and not ready:
                    failure = "SHUTDOWN"
                jobs.append((request, frames, failure))
            self._requests.clear()
            self._ring.clear()
            self._ring_bytes = 0
        outcomes: list[EvidenceOutcome] = []
        for request, frames, failure in jobs:
            if failure is not None or frames is None:
                outcomes.append(self._unavailable(request.event_id, failure or "SHUTDOWN"))
                continue
            timeout = self._policy.finalize_timeout_s
            if deadline is not None:
                timeout = min(timeout, deadline - self._monotonic())
            if timeout <= 0:
                outcomes.append(self._unavailable(request.event_id, "SHUTDOWN"))
            else:
                outcomes.append(self._encode(request.event_id, frames, timeout))
        return tuple(outcomes)

    # ------------------------------------------------------------------ internals

    def _start_segment(self, segment: int, timestamp_ms: int) -> None:
        if self._segment is not None:
            for request in self._requests.values():
                if request.failure is None:
                    request.failure = "SOURCE_DISCONTINUITY"
            self._evicted_frames += len(self._ring)
            self._ring.clear()
            self._ring_bytes = 0
        self._segment = segment
        self._segment_head_ts = timestamp_ms
        self._segment_head_evicted = False
        self._last_slot = None
        self._newest_ts = None
        self._source_ended = False

    def _evict(self) -> None:
        assert self._newest_ts is not None
        cut = self._newest_ts - (self._policy.pre_roll_ms + self._policy.retention_margin_ms)
        pins = [r.window_start_ms for r in self._requests.values() if r.failure is None]
        if pins:
            cut = min(cut, min(pins))
        # Keep the newest frame at or before `cut`, so the window start stays covered.
        while len(self._ring) >= 2 and self._ring[1].source_timestamp_ms <= cut:
            self._evict_oldest()
        overflowed = False
        while self._ring and (
            len(self._ring) > self._policy.max_ring_frames
            or self._ring_bytes > self._policy.max_ring_bytes
        ):
            self._evict_oldest()
            overflowed = True
        if overflowed:
            for request in self._requests.values():
                if request.failure is None and not self._start_covered(request):
                    request.failure = "RING_OVERFLOW"

    def _evict_oldest(self) -> None:
        frame = self._ring.popleft()
        self._ring_bytes -= len(frame.data)
        self._evicted_frames += 1
        if frame.source_timestamp_ms == self._segment_head_ts:
            self._segment_head_evicted = True

    def _start_covered(self, request: _Request) -> bool:
        if not self._ring:
            return False
        oldest = self._ring[0].source_timestamp_ms
        if oldest <= request.window_start_ms:
            return True
        return not self._segment_head_evicted and oldest == self._segment_head_ts

    def _select(
        self, request: _Request
    ) -> tuple[list[_EncodedFrame] | None, str | None, bool]:
        """Return (frames, failure_reason, ready) for a completed request; lock held."""

        assert request.window_end_ms is not None
        if request.failure is not None:
            return None, request.failure, False
        if not self._start_covered(request):
            return None, "PRE_ROLL_UNAVAILABLE", False
        newest = self._ring[-1].source_timestamp_ms
        if newest < request.window_end_ms and not self._source_ended:
            return None, None, False
        frames = list(self._ring)
        start_index = 0
        for index, item in enumerate(frames):
            if item.source_timestamp_ms <= request.window_start_ms:
                start_index = index
            else:
                break
        end_index = len(frames) - 1
        for index in range(start_index, len(frames)):
            if frames[index].source_timestamp_ms >= request.window_end_ms:
                end_index = index
                break
        return frames[start_index : end_index + 1], None, True

    def _encode(
        self, event_id: UUID, frames: list[_EncodedFrame], timeout_s: float
    ) -> EvidenceOutcome:
        ffmpeg = self._which(FFMPEG_EXECUTABLE)
        if not ffmpeg:
            return self._unavailable(event_id, "FFMPEG_UNAVAILABLE")
        session_dir = self._evidence_dir / str(self._session_id)
        final_path = session_dir / f"{event_id}.mp4"
        partial_path = session_dir / f".{event_id}.mp4.partial"
        start_ms = frames[0].source_timestamp_ms
        end_ms = frames[-1].source_timestamp_ms
        argv = build_ffmpeg_argv(
            ffmpeg,
            frame_count=len(frames),
            duration_ms=end_ms - start_ms,
            codec=self._policy.video_codec,
            output=partial_path,
        )
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            result = self._runner(
                argv,
                input=b"".join(item.data for item in frames),
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            partial_path.unlink(missing_ok=True)
            return self._unavailable(event_id, "FFMPEG_TIMEOUT")
        except OSError:
            partial_path.unlink(missing_ok=True)
            return self._unavailable(event_id, "FFMPEG_FAILED")
        if (
            result.returncode != 0
            or not partial_path.is_file()
            or partial_path.stat().st_size == 0
        ):
            partial_path.unlink(missing_ok=True)
            return self._unavailable(event_id, "FFMPEG_FAILED")
        os.replace(partial_path, final_path)
        return EvidenceOutcome(
            event_id=event_id,
            status=EvidenceStatus.AVAILABLE,
            relative_path=final_path.relative_to(self._relative_root).as_posix(),
            start_source_ms=start_ms,
            end_source_ms=end_ms,
            frame_count=len(frames),
            reason=None,
        )

    @staticmethod
    def _unavailable(event_id: UUID, reason: str) -> EvidenceOutcome:
        return EvidenceOutcome(
            event_id=event_id,
            status=EvidenceStatus.UNAVAILABLE,
            relative_path=None,
            start_source_ms=None,
            end_source_ms=None,
            frame_count=0,
            reason=reason,
        )

    def _default_encode(self, image_bgr: NDArray[np.uint8]) -> bytes | None:
        success, encoded = cv2.imencode(
            ".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, self._policy.jpeg_quality]
        )
        return encoded.tobytes() if success else None

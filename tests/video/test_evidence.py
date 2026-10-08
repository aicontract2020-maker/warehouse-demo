from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
import pytest

from app.video.evidence import (
    EVIDENCE_UNAVAILABLE,
    EvidenceManager,
    EvidencePathError,
    EvidencePolicy,
    EvidenceStatus,
    build_ffmpeg_argv,
    resolve_evidence_path,
)
from app.vision.detector import FrameEnvelope

SESSION_ID = UUID(int=0xA1)
EVENT_ID = UUID(int=0xE1)
FFMPEG = "/opt/tools/ffmpeg"


class FakeRunner:
    """Stands in for subprocess.run; writes a fake MP4 to the requested output path."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        raise_timeout: bool = False,
        write_output: bool = True,
    ) -> None:
        self.returncode = returncode
        self.raise_timeout = raise_timeout
        self.write_output = write_output
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, kwargs))
        output = Path(argv[-1])
        if self.write_output:
            output.write_bytes(b"\x00\x00\x00\x18ftypisom-fake-clip")
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
        return subprocess.CompletedProcess(argv, self.returncode, b"", b"encoder said no")


def fake_jpeg(image: np.ndarray) -> bytes:
    return b"JPEG" + bytes([int(image[0, 0, 0])]) + b"." * 95  # always 100 bytes


def frame(timestamp_ms: int, sequence: int | None = None, segment: int = 0) -> FrameEnvelope:
    return FrameEnvelope(
        session_id=SESSION_ID,
        continuity_segment=segment,
        sequence=timestamp_ms // 100 if sequence is None else sequence,
        source_timestamp_ms=timestamp_ms,
        captured_monotonic_ns=timestamp_ms * 1_000_000,
        image_bgr=np.full((24, 32, 3), (timestamp_ms // 100) % 256, dtype=np.uint8),
    )


def make_manager(
    tmp_path: Path,
    runner: Callable[..., Any] | None = None,
    *,
    ffmpeg: str | None = FFMPEG,
    policy: EvidencePolicy | None = None,
    clock: Callable[[], float] | None = None,
) -> EvidenceManager:
    evidence_dir = tmp_path / "data" / "evidence"
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["monotonic"] = clock
    return EvidenceManager(
        evidence_dir,
        SESSION_ID,
        policy or EvidencePolicy(),
        runner=runner or FakeRunner(),
        which=lambda name: ffmpeg,
        encode_jpeg=fake_jpeg,
        **kwargs,
    )


def feed(manager: EvidenceManager, start_ms: int, stop_ms: int, step_ms: int = 100) -> None:
    for timestamp_ms in range(start_ms, stop_ms + 1, step_ms):
        manager.add_frame(frame(timestamp_ms))


# --------------------------------------------------------------------------- policy


@pytest.mark.parametrize(
    "changes",
    [
        {"pre_roll_ms": 1_999},
        {"post_roll_ms": 1_000},
        {"max_ring_frames": 0},
        {"max_ring_bytes": 0},
        {"capture_fps": 0},
        {"finalize_timeout_s": float("inf")},
        {"finalize_timeout_s": 0},
        {"jpeg_quality": 101},
        {"video_codec": "libx264; rm -rf /"},
    ],
)
def test_policy_rejects_unsafe_or_out_of_contract_values(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        EvidencePolicy(**changes)


def test_default_policy_matches_two_second_pre_and_post_roll() -> None:
    policy = EvidencePolicy()

    assert (policy.pre_roll_ms, policy.post_roll_ms) == (2_000, 2_000)
    assert policy.video_codec == "libx264"


# --------------------------------------------------------------------------- bounded ring


def test_idle_ring_retains_only_pre_roll_plus_margin() -> None:
    manager = make_manager(Path("/nonexistent"))

    feed(manager, 0, 20_000)

    metrics = manager.metrics
    span = metrics.newest_source_ms - metrics.oldest_source_ms
    policy = EvidencePolicy()
    assert policy.pre_roll_ms <= span <= policy.pre_roll_ms + policy.retention_margin_ms + 100
    assert metrics.ring_frames == span // 100 + 1
    assert metrics.ring_bytes == metrics.ring_frames * 100
    assert metrics.evicted_frames == 201 - metrics.ring_frames


def test_ring_samples_down_to_capture_fps() -> None:
    manager = make_manager(Path("/nonexistent"), policy=EvidencePolicy(capture_fps=10))

    for index in range(0, 31):  # one second at ~30 FPS
        manager.add_frame(frame(index * 33, sequence=index))

    assert manager.metrics.ring_frames == 10
    assert manager.metrics.skipped_frames == 21


def test_hard_frame_and_byte_caps_bound_the_ring_even_while_pinned(tmp_path: Path) -> None:
    policy = EvidencePolicy(max_ring_frames=30, max_ring_bytes=2_500)
    manager = make_manager(tmp_path, policy=policy)
    feed(manager, 0, 1_000)
    manager.begin_event(EVENT_ID, first_action_source_ms=1_000)

    feed(manager, 1_100, 10_000)

    assert manager.metrics.ring_frames <= 25
    assert manager.metrics.ring_bytes <= 2_500


def test_ring_overflow_during_event_makes_evidence_unavailable(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager = make_manager(tmp_path, runner, policy=EvidencePolicy(max_ring_frames=40))
    feed(manager, 0, 3_000)
    manager.begin_event(EVENT_ID, first_action_source_ms=3_000)
    feed(manager, 3_100, 9_000)
    manager.complete_event(EVENT_ID, ended_source_ms=8_000)
    feed(manager, 9_100, 10_000)

    (outcome,) = manager.finalize_ready()

    assert outcome.status is EvidenceStatus.UNAVAILABLE
    assert outcome.reason == "RING_OVERFLOW"
    assert outcome.relative_path is None
    assert runner.calls == []


# --------------------------------------------------------------------------- pre/post roll


def test_clip_covers_two_seconds_before_first_action_through_two_after_completion(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    manager = make_manager(tmp_path, runner)
    feed(manager, 0, 3_000)
    manager.begin_event(EVENT_ID, first_action_source_ms=3_000)
    feed(manager, 3_100, 6_000)
    manager.complete_event(EVENT_ID, ended_source_ms=6_000)
    feed(manager, 6_100, 7_900)

    assert manager.finalize_ready() == ()  # post-roll not yet captured
    assert runner.calls == []

    manager.add_frame(frame(8_000))
    (outcome,) = manager.finalize_ready()

    assert outcome.status is EvidenceStatus.AVAILABLE
    assert outcome.event_id == EVENT_ID
    assert (outcome.start_source_ms, outcome.end_source_ms) == (1_000, 8_000)
    assert outcome.frame_count == 71
    assert outcome.reason is None
    assert outcome.integrity_flags == frozenset()
    _argv, kwargs = runner.calls[0]
    assert kwargs["input"] == b"".join(
        fake_jpeg(frame(timestamp).image_bgr) for timestamp in range(1_000, 8_001, 100)
    )


def test_pre_roll_is_clamped_to_the_first_frame_the_source_produced(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    feed(manager, 0, 500)
    manager.begin_event(EVENT_ID, first_action_source_ms=500)
    feed(manager, 600, 1_000)
    manager.complete_event(EVENT_ID, ended_source_ms=1_000)
    feed(manager, 1_100, 3_000)

    (outcome,) = manager.finalize_ready()

    assert outcome.status is EvidenceStatus.AVAILABLE
    assert outcome.start_source_ms == 0


def test_late_event_start_after_pre_roll_eviction_is_unavailable(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager = make_manager(tmp_path, runner)
    feed(manager, 0, 9_000)  # ring now holds roughly 5_000..9_000
    manager.begin_event(EVENT_ID, first_action_source_ms=3_000)
    manager.complete_event(EVENT_ID, ended_source_ms=9_000)
    feed(manager, 9_100, 11_000)

    (outcome,) = manager.finalize_ready()

    assert outcome.status is EvidenceStatus.UNAVAILABLE
    assert outcome.reason == "PRE_ROLL_UNAVAILABLE"
    assert outcome.integrity_flags == frozenset({EVIDENCE_UNAVAILABLE})
    assert runner.calls == []


def test_source_end_clamps_post_roll_to_the_last_frame(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    feed(manager, 0, 3_000)
    manager.begin_event(EVENT_ID, first_action_source_ms=3_000)
    feed(manager, 3_100, 6_000)
    manager.complete_event(EVENT_ID, ended_source_ms=5_500)

    assert manager.finalize_ready() == ()
    manager.mark_source_ended()
    (outcome,) = manager.finalize_ready()

    assert outcome.status is EvidenceStatus.AVAILABLE
    assert outcome.end_source_ms == 6_000


def test_continuity_segment_change_makes_open_evidence_unavailable(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager = make_manager(tmp_path, runner)
    feed(manager, 0, 3_000)
    manager.begin_event(EVENT_ID, first_action_source_ms=3_000)
    feed(manager, 3_100, 4_000)
    manager.complete_event(EVENT_ID, ended_source_ms=4_000)

    for timestamp_ms in range(5_000, 8_001, 100):
        manager.add_frame(frame(timestamp_ms, segment=1))
    (outcome,) = manager.finalize_ready()

    assert outcome.status is EvidenceStatus.UNAVAILABLE
    assert outcome.reason == "SOURCE_DISCONTINUITY"
    assert runner.calls == []
    assert manager.metrics.oldest_source_ms >= 5_000  # previous segment frames dropped


def test_abandoned_event_reports_discarded_without_encoding(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager = make_manager(tmp_path, runner)
    feed(manager, 0, 3_000)
    manager.begin_event(EVENT_ID, first_action_source_ms=3_000)

    outcome = manager.abandon_event(EVENT_ID, "DISCARDED")

    assert outcome.status is EvidenceStatus.UNAVAILABLE
    assert outcome.reason == "DISCARDED"
    assert manager.finalize_ready() == ()
    assert runner.calls == []


def test_unknown_event_completion_is_rejected(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    with pytest.raises(KeyError):
        manager.complete_event(UUID(int=404), ended_source_ms=1_000)


# --------------------------------------------------------------------------- FFmpeg control


def test_ffmpeg_is_invoked_with_a_controlled_argv_list_and_no_shell(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager = make_manager(tmp_path, runner)
    feed(manager, 0, 3_000)
    manager.begin_event(EVENT_ID, first_action_source_ms=3_000)
    manager.complete_event(EVENT_ID, ended_source_ms=3_000)
    feed(manager, 3_100, 5_000)

    manager.finalize_ready()

    (argv, kwargs) = runner.calls[0]
    assert isinstance(argv, list) and all(isinstance(item, str) for item in argv)
    assert argv[0] == FFMPEG
    assert kwargs.get("shell", False) is False
    assert kwargs["timeout"] == EvidencePolicy().finalize_timeout_s
    assert kwargs["check"] is False
    assert isinstance(kwargs["input"], bytes)
    joined = " ".join(argv)
    for expected in (
        "-nostdin",
        "-f image2pipe",
        "-c:v mjpeg",
        "-i pipe:0",
        "-an",
        "-c:v libx264",
        "-pix_fmt yuv420p",
        "-movflags +faststart",
        "-f mp4",
    ):
        if expected == "-nostdin":
            assert expected not in argv  # stdin carries the frames
        else:
            assert expected in joined
    output = Path(argv[-1])
    assert output.parent == tmp_path / "data" / "evidence" / str(SESSION_ID)
    assert output.name.endswith(".partial")
    assert str(EVENT_ID) in output.name
    rate = argv[argv.index("-framerate") + 1]
    numerator, denominator = (int(part) for part in rate.split("/"))
    assert numerator / denominator == pytest.approx(10.0)


def test_build_ffmpeg_argv_rejects_unlisted_codecs_and_bad_rates(tmp_path: Path) -> None:
    out = tmp_path / "clip.partial"
    argv = build_ffmpeg_argv(FFMPEG, frame_count=11, duration_ms=1_000, codec="libx264", output=out)
    assert argv[0] == FFMPEG and argv[-1] == str(out)

    with pytest.raises(ValueError):
        build_ffmpeg_argv(FFMPEG, frame_count=11, duration_ms=1_000, codec="-vf x", output=out)
    with pytest.raises(ValueError):
        build_ffmpeg_argv(FFMPEG, frame_count=0, duration_ms=1_000, codec="libx264", output=out)


def test_successful_clip_is_atomically_placed_with_relative_manifest_path(
    tmp_path: Path,
) -> None:
    manager = make_manager(tmp_path)
    feed(manager, 0, 3_000)
    manager.begin_event(EVENT_ID, first_action_source_ms=3_000)
    manager.complete_event(EVENT_ID, ended_source_ms=3_500)
    feed(manager, 3_100, 5_500)

    (outcome,) = manager.finalize_ready()

    assert outcome.relative_path == f"evidence/{SESSION_ID}/{EVENT_ID}.mp4"
    assert outcome.evidence_path == outcome.relative_path
    assert not Path(outcome.relative_path).is_absolute()
    resolved = resolve_evidence_path(tmp_path / "data" / "evidence", outcome.relative_path)
    assert resolved == (tmp_path / "data" / "evidence" / str(SESSION_ID) / f"{EVENT_ID}.mp4")
    assert resolved.read_bytes().startswith(b"\x00\x00\x00\x18ftyp")
    assert list(resolved.parent.glob("*.partial")) == []


@pytest.mark.parametrize(
    "relative_path",
    ["../secrets.mp4", "evidence/../../x.mp4", "/etc/passwd", "uploads/x.mp4", ""],
)
def test_evidence_path_resolution_refuses_paths_outside_evidence_dir(
    tmp_path: Path, relative_path: str
) -> None:
    with pytest.raises(EvidencePathError) as failure:
        resolve_evidence_path(tmp_path / "data" / "evidence", relative_path)

    assert failure.value.code == "PATH_NOT_ALLOWED"


def test_missing_ffmpeg_marks_evidence_unavailable_without_running_anything(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    manager = make_manager(tmp_path, runner, ffmpeg=None)
    feed(manager, 0, 3_000)
    manager.begin_event(EVENT_ID, first_action_source_ms=3_000)
    manager.complete_event(EVENT_ID, ended_source_ms=3_000)
    feed(manager, 3_100, 5_000)

    (outcome,) = manager.finalize_ready()

    assert outcome.status is EvidenceStatus.UNAVAILABLE
    assert outcome.reason == "FFMPEG_UNAVAILABLE"
    assert outcome.integrity_flags == frozenset({EVIDENCE_UNAVAILABLE})
    assert runner.calls == []


@pytest.mark.parametrize(
    ("runner", "reason"),
    [
        (FakeRunner(returncode=1), "FFMPEG_FAILED"),
        (FakeRunner(raise_timeout=True), "FFMPEG_TIMEOUT"),
        (FakeRunner(write_output=False), "FFMPEG_FAILED"),
    ],
)
def test_encoder_failure_or_timeout_leaves_no_partial_file(
    tmp_path: Path, runner: FakeRunner, reason: str
) -> None:
    manager = make_manager(tmp_path, runner)
    feed(manager, 0, 3_000)
    manager.begin_event(EVENT_ID, first_action_source_ms=3_000)
    manager.complete_event(EVENT_ID, ended_source_ms=3_000)
    feed(manager, 3_100, 5_000)

    (outcome,) = manager.finalize_ready()

    assert outcome.status is EvidenceStatus.UNAVAILABLE
    assert outcome.reason == reason
    session_dir = tmp_path / "data" / "evidence" / str(SESSION_ID)
    assert not session_dir.exists() or list(session_dir.iterdir()) == []


# --------------------------------------------------------------------------- shutdown


def test_shutdown_finalizes_ready_clips_and_abandons_waiting_ones(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager = make_manager(tmp_path, runner)
    ready_id, waiting_id, active_id = UUID(int=1), UUID(int=2), UUID(int=3)
    feed(manager, 0, 2_000)
    manager.begin_event(ready_id, first_action_source_ms=2_000)
    manager.complete_event(ready_id, ended_source_ms=2_000)
    feed(manager, 2_100, 4_000)
    manager.begin_event(waiting_id, first_action_source_ms=4_000)
    manager.complete_event(waiting_id, ended_source_ms=4_000)
    manager.begin_event(active_id, first_action_source_ms=4_000)

    outcomes = {outcome.event_id: outcome for outcome in manager.shutdown(timeout_s=5.0)}

    assert outcomes[ready_id].status is EvidenceStatus.AVAILABLE
    assert outcomes[waiting_id].reason == "SHUTDOWN"
    assert outcomes[active_id].reason == "SHUTDOWN"
    assert len(runner.calls) == 1
    assert manager.metrics.ring_frames == 0
    assert manager.add_frame(frame(4_100)) is False
    assert manager.shutdown(timeout_s=5.0) == ()


def test_shutdown_bounds_encoder_time_by_remaining_deadline(tmp_path: Path) -> None:
    now = [100.0]
    runner = FakeRunner()
    manager = make_manager(tmp_path, runner, clock=lambda: now[0])
    feed(manager, 0, 2_000)
    manager.begin_event(EVENT_ID, first_action_source_ms=2_000)
    manager.complete_event(EVENT_ID, ended_source_ms=2_000)
    feed(manager, 2_100, 4_000)

    manager.shutdown(timeout_s=1.5)

    assert runner.calls[0][1]["timeout"] == pytest.approx(1.5)


# --------------------------------------------------------------------------- real FFmpeg


def _has_libx264(ffmpeg: str | None) -> bool:
    if ffmpeg is None:
        return False
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, timeout=10, check=False
    )
    return b"libx264" in result.stdout


@pytest.mark.skipif(not _has_libx264(shutil.which("ffmpeg")), reason="FFmpeg/libx264 missing")
def test_real_ffmpeg_produces_an_mp4_with_the_controlled_arguments(tmp_path: Path) -> None:
    manager = EvidenceManager(tmp_path / "data" / "evidence", SESSION_ID, EvidencePolicy())
    feed(manager, 0, 2_000)
    manager.begin_event(EVENT_ID, first_action_source_ms=2_000)
    manager.complete_event(EVENT_ID, ended_source_ms=2_500)
    feed(manager, 2_600, 4_500)

    (outcome,) = manager.finalize_ready()

    assert outcome.status is EvidenceStatus.AVAILABLE, outcome.reason
    assert outcome.relative_path is not None
    clip = resolve_evidence_path(tmp_path / "data" / "evidence", outcome.relative_path)
    assert clip.read_bytes()[4:8] == b"ftyp"

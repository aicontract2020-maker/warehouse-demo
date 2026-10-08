from __future__ import annotations

import dataclasses
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import cv2
import numpy as np
import pytest

from app.domain.zones import ZoneProfile
from app.services.runtime_state import (
    ActionView,
    Alert,
    AnalysisView,
    DetectionView,
    EventView,
    InventoryView,
    RuntimeStateStore,
    TaskView,
    TrackView,
)
from app.vision.detector import FrameEnvelope
from app.vision.overlay import (
    ACTION_COLOR,
    AMBIGUOUS_COLOR,
    BANNER_HEIGHT,
    DETECTION_COLOR,
    EXIT_COLOR,
    LINE_COLOR,
    SHELF_COLOR,
    TRACK_COLOR,
    PreviewRenderer,
    encode_jpeg,
    render_preview,
)

SESSION = UUID(int=0x51)
OTHER_SESSION = UUID(int=0x52)
EVENT = UUID(int=0xE1)
T0 = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)

LIVE_PAYLOAD_KEYS = {
    "monitoring_state",
    "source",
    "analysis",
    "event",
    "task",
    "inventory",
    "metrics",
    "alerts",
}
LIVE_METRIC_KEYS = {
    "capture_fps",
    "analysis_fps",
    "preview_fps",
    "dropped_frames",
    "late_results",
    "result_latency_ms",
    "analysis_queue_depth",
    "persistence_queue_depth",
}


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def zone() -> ZoneProfile:
    return ZoneProfile(
        zone_profile_id="zone-1",
        source_profile_id="source-1",
        name="A-03-01",
        version=1,
        source_width=1280,
        source_height=720,
        shelf_polygon=((0.05, 0.1), (0.45, 0.1), (0.45, 0.85), (0.05, 0.85)),
        interaction_polygon=((0.2, 0.15), (0.8, 0.15), (0.8, 0.95), (0.2, 0.95)),
        exit_polygon=((0.55, 0.1), (0.95, 0.1), (0.95, 0.85), (0.55, 0.85)),
        counting_line=((0.5, 0.2), (0.5, 0.9)),
        shelf_side_sign=1,
        uncertainty_band_norm=0.02,
        crossing_confirm_frames=2,
        quiet_seconds=3,
        stable_seconds=2,
        hard_idle_seconds=10,
        merge_window_seconds=10,
        active=True,
    )


def analysis(
    sequence: int,
    *,
    segment: int = 0,
    session_id: UUID = SESSION,
    detections: tuple[DetectionView, ...] = (),
    tracks: tuple[TrackView, ...] = (),
    actions: tuple[ActionView, ...] = (),
) -> AnalysisView:
    return AnalysisView(
        session_id=session_id,
        continuity_segment=segment,
        last_applied_frame_sequence=sequence,
        source_timestamp_ms=sequence * 250,
        detections=detections,
        tracks=tracks,
        actions=actions,
    )


def frame(sequence: int, session_id: UUID = SESSION, value: int = 0) -> FrameEnvelope:
    return FrameEnvelope(
        session_id=session_id,
        continuity_segment=0,
        sequence=sequence,
        source_timestamp_ms=sequence * 33,
        captured_monotonic_ns=sequence,
        image_bgr=np.full((720, 1280, 3), value, dtype=np.uint8),
    )


def detection(bbox: tuple[float, float, float, float] = (640, 300, 740, 420)) -> DetectionView:
    return DetectionView("18:0", 0, "bag", 0.98, bbox)


def track(
    track_id: int = 4,
    bbox: tuple[float, float, float, float] = (200, 200, 300, 320),
    *,
    ambiguous: bool = False,
) -> TrackView:
    return TrackView(track_id, "bag", 0.97, bbox, "confirmed", "shelf", ambiguous)


# --------------------------------------------------------------------------- snapshots


def test_initial_snapshot_contains_every_live_contract_field() -> None:
    store = RuntimeStateStore(now=Clock())

    payload = store.snapshot().to_live_payload()

    assert set(payload) == LIVE_PAYLOAD_KEYS
    assert payload["monitoring_state"] == "stopped"
    assert set(payload["source"]) == {
        "state",
        "profile_id",
        "source_timestamp_ms",
        "continuity_segment",
    }
    assert payload["source"]["state"] == "closed"
    assert set(payload["analysis"]) == {"last_applied_frame_sequence", "detections", "tracks"}
    assert set(payload["event"]) == {
        "event_id",
        "state",
        "pick_count",
        "return_count",
        "net_quantity",
        "last_action_at",
        "review_reason",
    }
    assert payload["event"]["event_id"] is None and payload["event"]["state"] is None
    assert payload["task"] is None and payload["inventory"] is None
    assert set(payload["metrics"]) == LIVE_METRIC_KEYS
    assert payload["alerts"] == []


def test_populated_snapshot_serializes_exactly_like_the_live_contract() -> None:
    clock = Clock()
    store = RuntimeStateStore(now=clock)
    store.update_monitoring(state="running", session_id=SESSION, source_profile_id="source-1")
    store.update_source(state="connected", profile_id="source-1", source_timestamp_ms=4_300)
    store.publish_analysis(analysis(18, detections=(detection(),), tracks=(track(),)))
    store.update_event(
        EventView(EVENT, "active", 3, 1, 2, T0 + timedelta(seconds=4), None)
    )
    store.update_task(TaskView("PICK-1", "RICE-25KG-A", 8, "bag", "A-03-01", "open"))
    store.update_inventory(InventoryView("A-03-01", "RICE-25KG-A", 42, 1))
    store.update_metrics(capture_fps=29.7, analysis_fps=4.0, dropped_frames=83)
    store.raise_alert(Alert("EVIDENCE_DEGRADED", "warning", "FFmpeg missing"))

    payload = store.snapshot().to_live_payload()

    assert payload["monitoring_state"] == "running"
    assert payload["source"] == {
        "state": "connected",
        "profile_id": "source-1",
        "source_timestamp_ms": 4_300,
        "continuity_segment": 0,
    }
    assert payload["analysis"] == {
        "last_applied_frame_sequence": 18,
        "detections": [
            {
                "detection_id": "18:0",
                "class_id": 0,
                "class_name": "bag",
                "confidence": 0.98,
                "bbox": [640, 300, 740, 420],
            }
        ],
        "tracks": [
            {
                "track_id": 4,
                "class_name": "bag",
                "confidence": 0.97,
                "bbox": [200, 200, 300, 320],
                "state": "confirmed",
                "line_side": "shelf",
                "ambiguous": False,
            }
        ],
    }
    assert payload["event"] == {
        "event_id": str(EVENT),
        "state": "active",
        "pick_count": 3,
        "return_count": 1,
        "net_quantity": 2,
        "last_action_at": "2026-10-08T16:00:04Z",
        "review_reason": None,
    }
    assert payload["task"] == {
        "task_id": "PICK-1",
        "sku_id": "RICE-25KG-A",
        "expected_quantity": 8,
        "unit": "bag",
        "source_location_id": "A-03-01",
        "status": "open",
    }
    assert payload["inventory"] == {
        "location_id": "A-03-01",
        "sku_id": "RICE-25KG-A",
        "quantity": 42,
        "version": 1,
    }
    assert payload["metrics"]["capture_fps"] == 29.7
    assert payload["metrics"]["dropped_frames"] == 83
    assert payload["alerts"] == [
        {"code": "EVIDENCE_DEGRADED", "severity": "warning", "message": "FFmpeg missing"}
    ]


def test_snapshots_are_immutable_and_payloads_are_detached_copies() -> None:
    store = RuntimeStateStore(now=Clock())
    store.publish_analysis(analysis(1, detections=(detection(),)))
    snapshot = store.snapshot()

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.sequence = 99  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.metrics.dropped_frames = 5  # type: ignore[misc]
    assert isinstance(snapshot.analysis.detections, tuple)

    payload = snapshot.to_live_payload()
    payload["analysis"]["detections"].clear()
    payload["metrics"]["dropped_frames"] = 1_000

    again = store.snapshot().to_live_payload()
    assert len(again["analysis"]["detections"]) == 1
    assert again["metrics"]["dropped_frames"] == 0


def test_every_change_publishes_a_new_snapshot_with_increasing_sequence() -> None:
    store = RuntimeStateStore(now=Clock())
    first = store.snapshot()

    store.update_monitoring(state="starting")
    second = store.snapshot()
    store.update_metrics(analysis_fps=4.0)
    third = store.snapshot()

    assert first.sequence < second.sequence < third.sequence
    assert first.monitoring.state == "stopped"  # old snapshot unchanged
    assert second.monitoring.state == "starting"


def test_envelope_follows_live_contract() -> None:
    clock = Clock()
    store = RuntimeStateStore(now=clock)
    snapshot = store.snapshot()

    envelope = snapshot.to_envelope(sent_at=T0 + timedelta(milliseconds=250))

    assert envelope == {
        "type": "snapshot",
        "schema_version": 1,
        "sequence": snapshot.sequence,
        "sent_at": "2026-10-08T16:00:00.250000Z",
        "payload": snapshot.to_live_payload(),
    }


def test_active_event_id_and_status_fields_are_kept_for_status_endpoint() -> None:
    store = RuntimeStateStore(now=Clock())
    store.update_source(
        state="reconnecting", reconnect_attempts=3, error_code="SOURCE_RECONNECTING"
    )
    store.update_event(EventView(EVENT, "settling", 1, 0, 1, None, None))
    store.update_persistence(state="blocked")
    store.update_metrics(captured_frames=120, analyzed_frames=16, result_latency_p95_ms=420.0)

    snapshot = store.snapshot()

    assert snapshot.monitoring.active_event_id == EVENT
    assert snapshot.source.reconnect_attempts == 3
    assert snapshot.source.error_code == "SOURCE_RECONNECTING"
    assert snapshot.persistence.state == "blocked"
    assert snapshot.metrics.captured_frames == 120
    assert snapshot.metrics.result_latency_p95_ms == 420.0


# --------------------------------------------------------------------------- ordering


def test_older_or_equal_frame_sequences_are_rejected_and_counted_late() -> None:
    store = RuntimeStateStore(now=Clock())

    assert store.publish_analysis(analysis(5)) is True
    assert store.publish_analysis(analysis(5)) is False
    assert store.publish_analysis(analysis(3)) is False
    assert store.publish_analysis(analysis(6)) is True

    snapshot = store.snapshot()
    assert snapshot.analysis.last_applied_frame_sequence == 6
    assert snapshot.metrics.late_results == 2


def test_higher_continuity_segment_is_accepted_and_lower_segment_rejected() -> None:
    store = RuntimeStateStore(now=Clock())
    store.publish_analysis(analysis(40, segment=0))

    assert store.publish_analysis(analysis(41, segment=1)) is True
    assert store.publish_analysis(analysis(50, segment=0)) is False
    assert store.snapshot().source.continuity_segment == 1


def test_results_from_a_previous_session_are_rejected_after_reset() -> None:
    store = RuntimeStateStore(now=Clock())
    store.publish_analysis(analysis(10))
    store.publish_frame(frame(10))

    store.reset_session(OTHER_SESSION)

    assert store.snapshot().analysis.last_applied_frame_sequence is None
    assert store.latest_frame() is None
    assert store.publish_analysis(analysis(11, session_id=SESSION)) is False
    assert store.publish_frame(frame(12, session_id=SESSION)) is False
    assert store.publish_analysis(analysis(0, session_id=OTHER_SESSION)) is True


def test_latest_frame_wins_and_older_frames_are_ignored() -> None:
    store = RuntimeStateStore(now=Clock())

    assert store.publish_frame(frame(3, value=3)) is True
    assert store.publish_frame(frame(2, value=2)) is False
    latest = store.latest_frame()

    assert latest is not None and latest.sequence == 3


# --------------------------------------------------------------------------- latest-only


def test_slow_subscriber_receives_only_the_newest_snapshot() -> None:
    store = RuntimeStateStore(now=Clock())
    subscription = store.subscribe()

    initial = subscription.get(timeout=0.1)
    for value in range(1, 6):
        store.update_metrics(dropped_frames=value)
    newest = subscription.get(timeout=0.1)

    assert initial is not None and initial.metrics.dropped_frames == 0
    assert newest is not None and newest.metrics.dropped_frames == 5
    assert subscription.dropped == 4
    assert subscription.get(timeout=0.01) is None  # nothing newer pending


def test_each_subscriber_is_independent_and_close_unblocks_waiters() -> None:
    store = RuntimeStateStore(now=Clock())
    fast = store.subscribe()
    slow = store.subscribe()
    fast.get(timeout=0.1)
    slow.get(timeout=0.1)

    store.update_metrics(dropped_frames=1)
    assert fast.get(timeout=0.1).metrics.dropped_frames == 1  # type: ignore[union-attr]
    store.update_metrics(dropped_frames=2)

    assert slow.get(timeout=0.1).metrics.dropped_frames == 2  # type: ignore[union-attr]
    assert fast.get(timeout=0.1).metrics.dropped_frames == 2  # type: ignore[union-attr]

    results: list[object] = []
    waiter = threading.Thread(target=lambda: results.append(fast.get(timeout=5)))
    waiter.start()
    time.sleep(0.02)
    fast.close()
    waiter.join(timeout=1)
    assert not waiter.is_alive() and results == [None]
    assert store.subscriber_count == 1


def test_preview_jpeg_publication_keeps_only_the_latest_image() -> None:
    store = RuntimeStateStore(now=Clock())

    store.publish_preview(b"\xff\xd8one", frame_sequence=1)
    store.publish_preview(b"\xff\xd8two", frame_sequence=2)
    store.publish_preview(b"\xff\xd8old", frame_sequence=1)
    preview = store.preview()

    assert preview is not None
    assert preview.jpeg == b"\xff\xd8two"
    assert preview.frame_sequence == 2
    waited = store.wait_preview(after_version=preview.version, timeout=0.01)
    assert waited is None


# --------------------------------------------------------------------------- overlay


def color_at(image: np.ndarray, x: int, y: int) -> tuple[int, int, int]:
    blue, green, red = image[y, x]
    return int(blue), int(green), int(red)


def overlay_snapshot(*, actions: tuple[ActionView, ...] = (), net: int = 0):
    store = RuntimeStateStore(now=Clock())
    store.update_monitoring(state="running", session_id=SESSION)
    store.update_source(state="connected")
    store.publish_analysis(
        analysis(
            7,
            detections=(detection((640, 300, 740, 420)),),
            tracks=(
                track(4, (900, 400, 1000, 520)),
                track(5, (300, 500, 400, 600), ambiguous=True),
            ),
            actions=actions,
        )
    )
    store.update_event(EventView(EVENT, "active", max(net, 0), 0, net, None, None))
    return store.snapshot()


def test_overlay_draws_zone_regions_and_counting_line_without_mutating_input() -> None:
    image = np.zeros((720, 1280, 3), dtype=np.uint8)

    rendered = render_preview(image, overlay_snapshot(), zone(), max_width=1280)

    assert rendered.shape == (720, 1280, 3)
    assert not image.any()
    # shelf top edge y = 0.1 * 720 = 72, x = 0.25 * 1280 = 320
    assert color_at(rendered, 320, 72) == SHELF_COLOR
    # exit right edge x = 0.95 * 1280 = 1216, y = 0.5 * 720 = 360
    assert color_at(rendered, 1216, 360) == EXIT_COLOR
    # counting line x = 640 runs from y=144 to y=648; y=600 is outside every box
    assert color_at(rendered, 640, 600) == LINE_COLOR


def test_overlay_draws_detections_tracks_ambiguity_and_labels() -> None:
    rendered = render_preview(
        np.zeros((720, 1280, 3), dtype=np.uint8), overlay_snapshot(), zone(), max_width=1280
    )

    assert color_at(rendered, 690, 420) == DETECTION_COLOR  # detection bottom edge
    assert color_at(rendered, 950, 520) == TRACK_COLOR  # confirmed track bottom edge
    assert color_at(rendered, 350, 600) == AMBIGUOUS_COLOR  # ambiguous track bottom edge
    label_area = rendered[380:400, 900:1000]  # track ID label above the box
    assert label_area.any()


def test_overlay_marks_directional_actions() -> None:
    action = ActionView(UUID(int=1), 4, "pick", 1, (900, 400, 1000, 520), 1_750)
    plain = render_preview(
        np.zeros((720, 1280, 3), dtype=np.uint8), overlay_snapshot(), zone(), max_width=1280
    )
    marked = render_preview(
        np.zeros((720, 1280, 3), dtype=np.uint8),
        overlay_snapshot(actions=(action,)),
        zone(),
        max_width=1280,
    )

    difference = np.any(marked != plain, axis=2)
    _, xs = np.nonzero(difference)
    assert len(xs) > 0
    assert xs.min() >= 850 and xs.max() <= 1100  # marker stays near the action's box
    assert (marked[difference] == np.array(ACTION_COLOR, dtype=np.uint8)).all(axis=1).any()


def test_status_banner_shows_event_state_and_count_on_top_of_everything() -> None:
    snapshot_two = overlay_snapshot(net=2)
    snapshot_three = overlay_snapshot(net=3)
    bright = np.full((720, 1280, 3), 255, dtype=np.uint8)

    two = render_preview(bright, snapshot_two, zone(), max_width=1280)
    three = render_preview(bright, snapshot_three, zone(), max_width=1280)

    banner_two = two[:BANNER_HEIGHT]
    assert banner_two.mean() < 128  # dark banner keeps text readable over any frame
    assert not np.array_equal(banner_two, three[:BANNER_HEIGHT])  # count text changes
    assert np.array_equal(two[BANNER_HEIGHT + 200 :], three[BANNER_HEIGHT + 200 :])


def test_overlay_scales_large_frames_down_to_the_preview_width() -> None:
    rendered = render_preview(
        np.zeros((720, 1280, 3), dtype=np.uint8), overlay_snapshot(), zone(), max_width=640
    )

    assert rendered.shape == (360, 640, 3)
    assert color_at(rendered, 345, 210) == DETECTION_COLOR  # (690, 420) scaled by 0.5


def test_overlay_without_zone_or_analysis_still_renders_banner() -> None:
    store = RuntimeStateStore(now=Clock())

    rendered = render_preview(
        np.zeros((240, 320, 3), dtype=np.uint8), store.snapshot(), None, max_width=640
    )

    assert rendered.shape == (240, 320, 3)
    assert rendered[:BANNER_HEIGHT].any()


def test_jpeg_generation_produces_decodable_images() -> None:
    image = render_preview(
        np.zeros((720, 1280, 3), dtype=np.uint8), overlay_snapshot(), zone(), max_width=960
    )

    data = encode_jpeg(image, quality=75)

    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"
    decoded = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == (540, 960, 3)
    with pytest.raises(ValueError):
        encode_jpeg(image, quality=0)


def test_preview_renderer_publishes_jpeg_for_the_latest_frame() -> None:
    store = RuntimeStateStore(now=Clock())
    store.update_monitoring(state="running", session_id=SESSION)
    store.publish_frame(frame(9))
    renderer = PreviewRenderer(store, zone_provider=zone, max_width=640, quality=70)

    assert renderer.render_latest() is True
    preview = store.preview()

    assert preview is not None and preview.frame_sequence == 9
    assert preview.jpeg[:2] == b"\xff\xd8"
    assert renderer.render_latest() is False  # nothing new to render

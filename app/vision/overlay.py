"""Annotated preview rendering: zones, counting line, detections, tracks, actions, status.

The renderer never mutates the source frame. It scales the frame to the preview width first
and then draws with scaled coordinates so line widths stay crisp. All shapes and text use
``cv2.LINE_8`` (no anti-aliasing) so overlay colors are exact. The status banner is drawn last
so no annotation can cover the count or event state, and every status is spelled out in text.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence

import cv2
import numpy as np
from numpy.typing import NDArray

from app.domain.zones import ZoneProfile
from app.services.runtime_state import RuntimeSnapshot, RuntimeStateStore

Color = tuple[int, int, int]  # BGR

SHELF_COLOR: Color = (255, 160, 0)
INTERACTION_COLOR: Color = (200, 200, 200)
EXIT_COLOR: Color = (0, 165, 255)
LINE_COLOR: Color = (0, 0, 255)
DETECTION_COLOR: Color = (0, 255, 255)
TRACK_COLOR: Color = (0, 255, 0)
AMBIGUOUS_COLOR: Color = (255, 0, 255)
ACTION_COLOR: Color = (255, 255, 0)
BANNER_COLOR: Color = (32, 32, 32)
TEXT_COLOR: Color = (255, 255, 255)

BANNER_HEIGHT = 32
DEFAULT_PREVIEW_WIDTH = 960
DEFAULT_JPEG_QUALITY = 80

_FONT = cv2.FONT_HERSHEY_SIMPLEX
_LINE = cv2.LINE_8
_LABEL_SCALE = 0.5
_BANNER_SCALE = 0.6

Image = NDArray[np.uint8]


def _scaled(image: Image, max_width: int) -> tuple[Image, float]:
    if max_width <= 0:
        raise ValueError("max_width must be positive")
    height, width = image.shape[:2]
    if width <= max_width:
        return image.copy(), 1.0
    scale = max_width / width
    size = (max_width, max(1, round(height * scale)))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA), scale


def _point(x: float, y: float, sx: float, sy: float) -> tuple[int, int]:
    return round(x * sx), round(y * sy)


def _label(image: Image, text: str, origin: tuple[int, int], color: Color) -> None:
    x, y = origin
    y = max(BANNER_HEIGHT + 14, y)
    cv2.putText(image, text, (x, y), _FONT, _LABEL_SCALE, color, 1, _LINE)


def _polygon(image: Image, points: Sequence[tuple[float, float]], color: Color) -> None:
    height, width = image.shape[:2]
    pixels = np.array(
        [(round(x * width), round(y * height)) for x, y in points], dtype=np.int32
    )
    cv2.polylines(image, [pixels], isClosed=True, color=color, thickness=2, lineType=_LINE)


def _draw_zone(image: Image, zone: ZoneProfile) -> None:
    height, width = image.shape[:2]
    regions = (
        ("INTERACTION", zone.interaction_polygon, INTERACTION_COLOR),
        ("SHELF", zone.shelf_polygon, SHELF_COLOR),
        ("EXIT", zone.exit_polygon, EXIT_COLOR),
    )
    for name, polygon, color in regions:
        _polygon(image, polygon, color)
        x = min(round(point[0] * width) for point in polygon)
        y = min(round(point[1] * height) for point in polygon)
        _label(image, name, (x + 6, y + 18), color)
    (x1, y1), (x2, y2) = zone.counting_line
    start = (round(x1 * width), round(y1 * height))
    end = (round(x2 * width), round(y2 * height))
    cv2.line(image, start, end, LINE_COLOR, 2, _LINE)
    _label(image, "COUNT LINE", (end[0] + 6, end[1]), LINE_COLOR)


def _box(
    image: Image,
    bbox: tuple[float, float, float, float],
    sx: float,
    sy: float,
    color: Color,
    label: str,
) -> tuple[int, int, int, int]:
    x1, y1 = _point(bbox[0], bbox[1], sx, sy)
    x2, y2 = _point(bbox[2], bbox[3], sx, sy)
    cv2.rectangle(image, (x1, y1), (x2, y2), color, 2, _LINE)
    _label(image, label, (x1, y1 - 6), color)
    return x1, y1, x2, y2


def _status_text(snapshot: RuntimeSnapshot) -> str:
    event = snapshot.event
    parts = [f"MONITOR {snapshot.monitoring.state.upper()}"]
    parts.append(f"SOURCE {snapshot.source.state.upper()}")
    if event.event_id is None:
        parts.append("EVENT IDLE")
    else:
        parts.append(f"EVENT {(event.state or 'unknown').upper()}")
    parts.append(f"PICK {event.pick_count} RET {event.return_count} NET {event.net_quantity:+d}")
    if event.review_reason:
        parts.append(f"REVIEW {event.review_reason}")
    if snapshot.persistence.state != "ready":
        parts.append(f"WORKBOOK {snapshot.persistence.state.upper()}")
    return " | ".join(parts)


def _draw_banner(image: Image, snapshot: RuntimeSnapshot) -> None:
    width = image.shape[1]
    banner_height = min(BANNER_HEIGHT, image.shape[0])
    cv2.rectangle(image, (0, 0), (width - 1, banner_height - 1), BANNER_COLOR, -1, _LINE)
    text = _status_text(snapshot)
    scale = _BANNER_SCALE
    (text_width, _), _ = cv2.getTextSize(text, _FONT, scale, 1)
    if text_width > width - 12:
        scale = max(0.3, scale * (width - 12) / text_width)
    baseline_y = min(banner_height - 10, 22)
    cv2.putText(image, text, (6, baseline_y), _FONT, scale, TEXT_COLOR, 1, _LINE)


def render_preview(
    image_bgr: Image,
    snapshot: RuntimeSnapshot,
    zone: ZoneProfile | None,
    max_width: int = DEFAULT_PREVIEW_WIDTH,
) -> Image:
    """Return an annotated, possibly downscaled copy of ``image_bgr``."""
    if image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
        raise ValueError("preview frames must be BGR images")
    canvas, scale = _scaled(image_bgr, max_width)
    sx = sy = scale
    if zone is not None:
        _draw_zone(canvas, zone)
    analysis = snapshot.analysis
    for detection in analysis.detections:
        label = f"{detection.class_name} {detection.confidence:.2f}"
        _box(canvas, detection.bbox, sx, sy, DETECTION_COLOR, label)
    for track in analysis.tracks:
        color = AMBIGUOUS_COLOR if track.ambiguous else TRACK_COLOR
        suffix = " ?" if track.ambiguous else ""
        _box(canvas, track.bbox, sx, sy, color, f"#{track.track_id} {track.state}{suffix}")
    for action in analysis.actions:
        x1, _ = _point(action.bbox[0], action.bbox[1], sx, sy)
        _, y2 = _point(action.bbox[2], action.bbox[3], sx, sy)
        text = f"{action.action_type.upper()} {action.delta:+d}"
        cv2.putText(canvas, text, (x1, y2 + 18), _FONT, _LABEL_SCALE, ACTION_COLOR, 1, _LINE)
    _draw_banner(canvas, snapshot)
    return canvas


def encode_jpeg(image: Image, quality: int = DEFAULT_JPEG_QUALITY) -> bytes:
    if not 1 <= quality <= 100:
        raise ValueError("JPEG quality must be between 1 and 100")
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return encoded.tobytes()


class PreviewRenderer:
    """Render the newest stored frame with the current snapshot and publish it as JPEG."""

    def __init__(
        self,
        store: RuntimeStateStore,
        zone_provider: Callable[[], ZoneProfile | None],
        max_width: int = DEFAULT_PREVIEW_WIDTH,
        quality: int = DEFAULT_JPEG_QUALITY,
    ) -> None:
        self._store = store
        self._zone_provider = zone_provider
        self._max_width = max_width
        self._quality = quality
        self._lock = threading.Lock()
        self._last: tuple[object, int, int] | None = None

    def render_latest(self) -> bool:
        with self._lock:
            frame = self._store.latest_frame()
            if frame is None:
                return False
            key = (frame.session_id, frame.continuity_segment, frame.sequence)
            if self._last == key:
                return False
            image = render_preview(
                frame.image_bgr, self._store.snapshot(), self._zone_provider(), self._max_width
            )
            published = self._store.publish_preview(
                encode_jpeg(image, self._quality), frame.sequence
            )
            self._last = key
            return published

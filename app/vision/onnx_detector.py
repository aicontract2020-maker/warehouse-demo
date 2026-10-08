"""YOLO-shaped ONNX detector adapter (plan sections 4.5, 7.1, 7.2).

Pipeline per frame: letterbox/stretch resize -> color order -> normalization -> layout ->
`session.run` -> output mapping -> confidence filter -> class allow-lists -> map boxes back to
source pixels and clip -> class-aware greedy NMS -> deterministic order -> max detections.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from app.vision.detector import (
    ClockNs,
    Detection,
    DetectionBatch,
    DetectionContext,
    DetectionFailure,
    FrameEnvelope,
)
from app.vision.model_manifest import ModelInputSpec, ModelManifest, verify_model_artifact

SessionFactory = Callable[[str], Any]

_ONNX_TYPES = {"float32": "tensor(float)", "uint8": "tensor(uint8)"}
_SCORE_TOLERANCE = 1e-4
_MIN_BOX_SIDE_PX = 1e-3


class OnnxModelError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def default_session_factory(model_path: str) -> Any:
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.log_severity_level = 3
    return ort.InferenceSession(
        model_path, sess_options=options, providers=["CPUExecutionProvider"]
    )


@dataclass(frozen=True, slots=True)
class ResizeTransform:
    """Maps model-input pixel coordinates back to source pixels."""

    scale_x: float
    scale_y: float
    pad_x: int
    pad_y: int
    source_width: int
    source_height: int


def preprocess(
    image_bgr: NDArray[np.uint8], spec: ModelInputSpec
) -> tuple[NDArray[Any], ResizeTransform]:
    source_height, source_width = image_bgr.shape[:2]
    target_width, target_height = spec.width, spec.height

    if spec.resize == "stretch":
        scale_x = target_width / source_width
        scale_y = target_height / source_height
        resized_width, resized_height = target_width, target_height
        pad_x = pad_y = 0
    else:
        ratio = min(target_width / source_width, target_height / source_height)
        scale_x = scale_y = ratio
        resized_width = max(1, round(source_width * ratio))
        resized_height = max(1, round(source_height * ratio))
        pad_x = (target_width - resized_width) // 2
        pad_y = (target_height - resized_height) // 2

    if (resized_width, resized_height) == (source_width, source_height):
        resized = image_bgr
    else:
        resized = cv2.resize(
            image_bgr, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR
        )
    canvas = np.full((target_height, target_width, 3), spec.pad_value, dtype=np.uint8)
    canvas[pad_y : pad_y + resized_height, pad_x : pad_x + resized_width] = resized
    if spec.color_order == "RGB":
        canvas = canvas[..., ::-1]

    if spec.dtype == "float32":
        tensor: NDArray[Any] = canvas.astype(np.float32) * np.float32(spec.scale)
        tensor = (tensor - np.asarray(spec.mean, dtype=np.float32)) / np.asarray(
            spec.std, dtype=np.float32
        )
    else:
        tensor = canvas.astype(np.uint8)
    if spec.layout == "NCHW":
        tensor = tensor.transpose(2, 0, 1)
    tensor = np.ascontiguousarray(tensor[None, ...])
    return tensor, ResizeTransform(
        scale_x=scale_x,
        scale_y=scale_y,
        pad_x=pad_x,
        pad_y=pad_y,
        source_width=source_width,
        source_height=source_height,
    )


def _iou(box: NDArray[np.float64], others: NDArray[np.float64]) -> NDArray[np.float64]:
    left = np.maximum(box[0], others[:, 0])
    top = np.maximum(box[1], others[:, 1])
    right = np.minimum(box[2], others[:, 2])
    bottom = np.minimum(box[3], others[:, 3])
    intersection = np.clip(right - left, 0, None) * np.clip(bottom - top, 0, None)
    area = (box[2] - box[0]) * (box[3] - box[1])
    other_areas = (others[:, 2] - others[:, 0]) * (others[:, 3] - others[:, 1])
    union = area + other_areas - intersection
    return np.divide(intersection, union, out=np.zeros_like(union), where=union > 0)


def non_max_suppression(
    boxes: NDArray[Any], scores: NDArray[Any], iou_threshold: float
) -> list[int]:
    """Greedy NMS. Ties in score keep the lower input index first, so output is stable."""

    if len(boxes) == 0:
        return []
    boxes64 = np.asarray(boxes, dtype=np.float64)
    order = sorted(range(len(scores)), key=lambda index: (-float(scores[index]), index))
    suppressed = np.zeros(len(boxes64), dtype=bool)
    keep: list[int] = []
    for index in order:
        if suppressed[index]:
            continue
        keep.append(index)
        overlaps = _iou(boxes64[index], boxes64)
        suppressed |= overlaps > iou_threshold
    return keep


class OnnxDetector:
    """`Detector` implementation backed by an onnxruntime-compatible session."""

    def __init__(
        self,
        manifest: ModelManifest,
        session: Any,
        *,
        clock_ns: ClockNs = time.monotonic_ns,
    ) -> None:
        self._manifest = manifest
        self._session = session
        self._clock_ns = clock_ns
        self._supported = frozenset(manifest.supported_classes)
        self._class_names = manifest.class_names
        self._validate_signature()

    @classmethod
    def from_manifest(
        cls,
        manifest_path: Path,
        *,
        session_factory: SessionFactory = default_session_factory,
        clock_ns: ClockNs = time.monotonic_ns,
    ) -> OnnxDetector:
        verified = verify_model_artifact(manifest_path)
        try:
            session = session_factory(str(verified.model_path))
        except Exception as error:
            raise OnnxModelError("MODEL_LOAD_FAILED", type(error).__name__) from error
        return cls(verified.manifest, session, clock_ns=clock_ns)

    @property
    def model_id(self) -> str:
        return self._manifest.model_id

    @property
    def manifest(self) -> ModelManifest:
        return self._manifest

    def detect(self, frame: FrameEnvelope, context: DetectionContext) -> DetectionBatch:
        started = self._clock_ns()
        tensor, transform = preprocess(frame.image_bgr, self._manifest.input)
        try:
            outputs = self._session.run(
                [self._manifest.output.name], {self._manifest.input.name: tensor}
            )
        except Exception as error:
            raise DetectionFailure(
                "INFERENCE_FAILED", type(error).__name__, frame.sequence
            ) from error
        detections = self._decode(outputs, transform, context, frame.sequence)
        completed = self._clock_ns()
        return DetectionBatch(
            session_id=frame.session_id,
            continuity_segment=frame.continuity_segment,
            frame_sequence=frame.sequence,
            source_timestamp_ms=frame.source_timestamp_ms,
            started_monotonic_ns=started,
            completed_monotonic_ns=completed,
            model_id=self._manifest.model_id,
            detections=detections,
        )

    def _validate_signature(self) -> None:
        spec = self._manifest.input
        inputs = list(self._session.get_inputs())
        if len(inputs) != 1:
            raise OnnxModelError("MODEL_SIGNATURE_MISMATCH", "model must have exactly one input")
        model_input = inputs[0]
        if model_input.name != spec.name:
            raise OnnxModelError("MODEL_SIGNATURE_MISMATCH", f"input name {model_input.name}")
        if model_input.type != _ONNX_TYPES[spec.dtype]:
            raise OnnxModelError("MODEL_SIGNATURE_MISMATCH", f"input type {model_input.type}")
        shape = list(model_input.shape)
        if len(shape) != 4:
            raise OnnxModelError("MODEL_SIGNATURE_MISMATCH", "input must be rank 4")
        for position, (actual, expected) in enumerate(zip(shape, spec.shape, strict=True)):
            symbolic = actual is None or isinstance(actual, str)
            if symbolic and position == 0:
                continue
            if actual != expected:
                raise OnnxModelError(
                    "MODEL_SIGNATURE_MISMATCH", f"input shape {shape} != {list(spec.shape)}"
                )
        output_names = {output.name for output in self._session.get_outputs()}
        if self._manifest.output.name not in output_names:
            raise OnnxModelError(
                "MODEL_SIGNATURE_MISMATCH", f"output {self._manifest.output.name} missing"
            )

    def _decode(
        self,
        outputs: Sequence[Any],
        transform: ResizeTransform,
        context: DetectionContext,
        frame_sequence: int,
    ) -> tuple[Detection, ...]:
        manifest = self._manifest
        output_spec = manifest.output

        def invalid(detail: str) -> DetectionFailure:
            return DetectionFailure("MODEL_OUTPUT_INVALID", detail, frame_sequence)

        if len(outputs) != 1:
            raise invalid("expected exactly one output tensor")
        raw = np.asarray(outputs[0])
        if raw.ndim != 3 or raw.shape[0] != 1 or not np.issubdtype(raw.dtype, np.floating):
            raise invalid(f"expected float tensor [1, A, N] or [1, N, A], got {raw.shape}")
        rows = raw[0].T if output_spec.layout == "attributes_first" else raw[0]
        if rows.shape[1] != manifest.attribute_count:
            raise invalid(f"expected {manifest.attribute_count} attributes, got {rows.shape[1]}")
        rows = rows.astype(np.float64)
        if not np.all(np.isfinite(rows)):
            raise invalid("output contains non-finite values")

        box_values = rows[:, :4]
        class_offset = 4 + int(output_spec.has_objectness)
        class_scores = rows[:, class_offset:]
        if output_spec.has_objectness:
            objectness = rows[:, 4]
            if np.any((objectness < -_SCORE_TOLERANCE) | (objectness > 1 + _SCORE_TOLERANCE)):
                raise invalid("objectness outside [0, 1]")
            class_scores = class_scores * objectness[:, None]
        if np.any((class_scores < -_SCORE_TOLERANCE) | (class_scores > 1 + _SCORE_TOLERANCE)):
            raise invalid("class scores outside [0, 1]")
        if len(rows) == 0:
            return ()

        class_ids = np.argmax(class_scores, axis=1)
        confidences = np.clip(class_scores[np.arange(len(rows)), class_ids], 0.0, 1.0)

        if output_spec.box_format == "cxcywh":
            cx, cy, width, height = box_values.T
            boxes = np.stack([cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2], 1)
        else:
            boxes = box_values.copy()
        if output_spec.coordinates == "normalized":
            boxes *= np.asarray(
                [manifest.input.width, manifest.input.height] * 2, dtype=np.float64
            )
        boxes[:, [0, 2]] = (boxes[:, [0, 2]] - transform.pad_x) / transform.scale_x
        boxes[:, [1, 3]] = (boxes[:, [1, 3]] - transform.pad_y) / transform.scale_y
        boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, transform.source_width)
        boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, transform.source_height)

        allowed_names = context.allowed_class_names & self._supported
        candidates = [
            index
            for index in range(len(rows))
            if confidences[index] >= context.threshold
            and self._class_names[class_ids[index]] in allowed_names
            and boxes[index, 2] - boxes[index, 0] > _MIN_BOX_SIDE_PX
            and boxes[index, 3] - boxes[index, 1] > _MIN_BOX_SIDE_PX
        ]
        candidates.sort(key=lambda index: self._order_key(index, confidences, class_ids, boxes))

        kept: list[int] = []
        for class_id in sorted({int(class_ids[index]) for index in candidates}):
            members = [index for index in candidates if int(class_ids[index]) == class_id]
            keep = non_max_suppression(
                boxes[members], confidences[members], manifest.defaults.nms_iou_threshold
            )
            kept.extend(members[position] for position in keep)
        kept.sort(key=lambda index: self._order_key(index, confidences, class_ids, boxes))
        kept = kept[: manifest.defaults.max_detections]

        return tuple(
            Detection(
                detection_id=f"{frame_sequence}:{ordinal}",
                class_id=int(class_ids[index]),
                class_name=self._class_names[class_ids[index]],
                confidence=float(confidences[index]),
                bbox_xyxy=(
                    float(boxes[index, 0]),
                    float(boxes[index, 1]),
                    float(boxes[index, 2]),
                    float(boxes[index, 3]),
                ),
            )
            for ordinal, index in enumerate(kept)
        )

    @staticmethod
    def _order_key(
        index: int,
        confidences: NDArray[np.float64],
        class_ids: NDArray[np.int64],
        boxes: NDArray[np.float64],
    ) -> tuple[float, int, float, float, float, float, int]:
        left, top, right, bottom = (float(value) for value in boxes[index])
        return (-float(confidences[index]), int(class_ids[index]), left, top, right, bottom, index)

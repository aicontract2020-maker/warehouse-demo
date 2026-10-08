from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
import pytest

from app.vision.detector import DetectionContext, DetectionFailure, FrameEnvelope
from app.vision.model_manifest import (
    ModelManifest,
    ModelManifestError,
    load_manifest,
    sha256_file,
    verify_model_artifact,
)
from app.vision.onnx_detector import (
    OnnxDetector,
    OnnxModelError,
    non_max_suppression,
    preprocess,
)

MODEL_BYTES = b"fixture-model-bytes"


# --------------------------------------------------------------------------- fixtures


def manifest_dict(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "model_id": "staged-goods",
        "version": "0.1.0",
        "sha256": hashlib.sha256(MODEL_BYTES).hexdigest(),
        "model_file": "model.onnx",
        "source": "trained in-house on staged demo goods",
        "license": {
            "name": "Apache-2.0",
            "source": "https://example.invalid/model-card",
            "commercial_use_allowed": True,
        },
        "input": {
            "name": "images",
            "shape": [1, 3, 8, 8],
            "layout": "NCHW",
            "dtype": "float32",
            "color_order": "RGB",
            "scale": 1 / 255,
            "mean": [0.0, 0.0, 0.0],
            "std": [1.0, 1.0, 1.0],
            "resize": "letterbox",
            "pad_value": 114,
        },
        "output": {
            "name": "output0",
            "layout": "attributes_first",
            "box_format": "cxcywh",
            "coordinates": "input_pixels",
            "has_objectness": False,
        },
        "classes": [{"id": 0, "name": "bag"}, {"id": 1, "name": "box"}],
        "supported_classes": ["bag", "box"],
        "defaults": {
            "confidence_threshold": 0.5,
            "nms_iou_threshold": 0.5,
            "max_detections": 50,
        },
        "validation": {"clip_set": "demo/model/validation-set.json", "metrics": {}},
    }
    for key, item in overrides.items():
        if isinstance(item, dict) and isinstance(value.get(key), dict):
            value[key] = {**value[key], **item}
        else:
            value[key] = item
    return value


def make_manifest(**overrides: Any) -> ModelManifest:
    return ModelManifest.model_validate(manifest_dict(**overrides))


def write_artifact(
    tmp_path: Path, model_bytes: bytes = MODEL_BYTES, **overrides: Any
) -> Path:
    data = manifest_dict(**overrides)
    if "sha256" not in overrides:
        data["sha256"] = hashlib.sha256(model_bytes).hexdigest()
    (tmp_path / "model.onnx").write_bytes(model_bytes)
    manifest_path = tmp_path / "model-manifest.json"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    return manifest_path


class FakeIO:
    def __init__(self, name: str, shape: Sequence[int | str | None], type_: str) -> None:
        self.name = name
        self.shape = list(shape)
        self.type = type_


class FakeSession:
    """Mimics the subset of onnxruntime.InferenceSession the detector uses."""

    def __init__(
        self,
        output: np.ndarray | Exception,
        *,
        inputs: Sequence[FakeIO] | None = None,
        outputs: Sequence[FakeIO] | None = None,
    ) -> None:
        self.output = output
        self.inputs = list(inputs or [FakeIO("images", [1, 3, 8, 8], "tensor(float)")])
        self.outputs = list(outputs or [FakeIO("output0", [1, 6, "anchors"], "tensor(float)")])
        self.feeds: list[dict[str, np.ndarray]] = []

    def get_inputs(self) -> list[FakeIO]:
        return self.inputs

    def get_outputs(self) -> list[FakeIO]:
        return self.outputs

    def run(self, names: Sequence[str], feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.feeds.append(feeds)
        if isinstance(self.output, Exception):
            raise self.output
        return [self.output]


def counter(start: int = 100, step: int = 10) -> Callable[[], int]:
    state = {"now": start - step}

    def tick() -> int:
        state["now"] += step
        return state["now"]

    return tick


def frame(width: int = 8, height: int = 8, sequence: int = 4) -> FrameEnvelope:
    return FrameEnvelope(
        session_id=UUID(int=5),
        continuity_segment=2,
        sequence=sequence,
        source_timestamp_ms=1_500,
        captured_monotonic_ns=50,
        image_bgr=np.zeros((height, width, 3), dtype=np.uint8),
    )


def context(threshold: float = 0.5, classes: Sequence[str] = ("bag", "box")) -> DetectionContext:
    return DetectionContext(allowed_class_names=frozenset(classes), threshold=threshold)


def yolo_v8(rows: Sequence[Sequence[float]]) -> np.ndarray:
    """rows are [cx, cy, w, h, score_class0, score_class1] -> tensor [1, 6, N]."""

    return np.asarray(rows, dtype=np.float32).reshape(-1, 6).T[None, ...]


# --------------------------------------------------------------------------- manifest gate


def test_verified_artifact_returns_manifest_path_and_matching_hash(tmp_path: Path) -> None:
    manifest_path = write_artifact(tmp_path)

    verified = verify_model_artifact(manifest_path)

    assert verified.manifest.model_id == "staged-goods"
    assert verified.model_path == (tmp_path / "model.onnx").resolve()
    assert verified.sha256 == hashlib.sha256(MODEL_BYTES).hexdigest()
    assert sha256_file(tmp_path / "model.onnx") == verified.sha256
    assert load_manifest(manifest_path) == verified.manifest


def test_hash_mismatch_is_refused_before_any_session_is_created(tmp_path: Path) -> None:
    manifest_path = write_artifact(tmp_path, sha256="0" * 64)
    created: list[str] = []

    with pytest.raises(ModelManifestError) as failure:
        OnnxDetector.from_manifest(
            manifest_path, session_factory=lambda path: created.append(path)
        )

    assert failure.value.code == "MODEL_HASH_MISMATCH"
    assert created == []


def test_missing_manifest_or_model_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ModelManifestError) as missing_manifest:
        verify_model_artifact(tmp_path / "absent.json")
    assert missing_manifest.value.code == "MANIFEST_MISSING"

    manifest_path = write_artifact(tmp_path)
    (tmp_path / "model.onnx").unlink()
    with pytest.raises(ModelManifestError) as missing_model:
        verify_model_artifact(manifest_path)
    assert missing_model.value.code == "MODEL_FILE_MISSING"


def test_unparseable_manifest_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "model-manifest.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ModelManifestError) as failure:
        load_manifest(path)

    assert failure.value.code == "MANIFEST_INVALID"


@pytest.mark.parametrize(
    "license_block",
    [
        {"name": "Apache-2.0", "source": "x", "commercial_use_allowed": False},
        {"name": "unknown", "source": "x", "commercial_use_allowed": True},
        {"name": "  ", "source": "x", "commercial_use_allowed": True},
        {"name": "CC-BY-NC-4.0", "source": "x", "commercial_use_allowed": True},
        {"name": "Apache-2.0", "source": "", "commercial_use_allowed": True},
    ],
)
def test_unknown_or_non_commercial_license_is_rejected(
    tmp_path: Path, license_block: dict[str, Any]
) -> None:
    manifest_path = write_artifact(tmp_path, license=license_block)

    with pytest.raises(ModelManifestError) as failure:
        verify_model_artifact(manifest_path)

    assert failure.value.code == "MODEL_LICENSE_REJECTED"


def test_manifest_without_license_block_is_invalid(tmp_path: Path) -> None:
    data = manifest_dict()
    del data["license"]
    path = tmp_path / "model-manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ModelManifestError) as failure:
        load_manifest(path)

    assert failure.value.code == "MANIFEST_INVALID"


@pytest.mark.parametrize("model_file", ["../model.onnx", "/tmp/model.onnx", "sub/../../x.onnx"])
def test_model_file_must_stay_inside_manifest_directory(tmp_path: Path, model_file: str) -> None:
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    manifest_path = write_artifact(artifact_dir, model_file=model_file)

    with pytest.raises(ModelManifestError) as failure:
        verify_model_artifact(manifest_path)

    assert failure.value.code == "MODEL_PATH_NOT_ALLOWED"


@pytest.mark.parametrize(
    "overrides",
    [
        {"sha256": "not-a-hash"},
        {"classes": [{"id": 0, "name": "bag"}, {"id": 0, "name": "box"}]},
        {"classes": [{"id": 0, "name": "bag"}, {"id": 2, "name": "box"}]},
        {"classes": [{"id": 0, "name": "bag"}, {"id": 1, "name": "bag"}]},
        {"supported_classes": ["bag", "pallet"]},
        {"input": {"shape": [1, 8, 8, 3]}},
        {"input": {"layout": "NHWC", "shape": [1, 3, 8, 8]}},
        {"input": {"shape": [2, 3, 8, 8]}},
        {"input": {"std": [1.0, 0.0, 1.0]}},
        {"input": {"dtype": "uint8"}},
        {"defaults": {"confidence_threshold": 1.5}},
        {"defaults": {"nms_iou_threshold": 0.0}},
        {"schema_version": 2},
        {"unexpected_field": True},
    ],
)
def test_malformed_manifest_fields_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        make_manifest(**overrides)


# --------------------------------------------------------------------------- preprocessing


def test_nchw_float_preprocess_converts_bgr_to_rgb_and_normalizes() -> None:
    manifest = make_manifest(
        input={"shape": [1, 3, 2, 2], "mean": [0.5, 0.25, 0.0], "std": [0.5, 0.5, 1.0]}
    )
    image = np.zeros((2, 2, 3), dtype=np.uint8)
    image[..., 0] = 255  # blue
    image[0, 0, 2] = 255  # red in the top-left pixel

    tensor, transform = preprocess(image, manifest.input)

    assert tensor.dtype == np.float32
    assert tensor.shape == (1, 3, 2, 2)
    assert tensor.flags["C_CONTIGUOUS"]
    red, green, blue = tensor[0]
    np.testing.assert_allclose(red, [[1.0, -1.0], [-1.0, -1.0]])
    np.testing.assert_allclose(green, np.full((2, 2), -0.5))
    np.testing.assert_allclose(blue, np.ones((2, 2)))
    assert (transform.scale_x, transform.scale_y, transform.pad_x, transform.pad_y) == (
        1.0,
        1.0,
        0,
        0,
    )


def test_nhwc_uint8_preprocess_keeps_bgr_bytes_without_normalization() -> None:
    manifest = make_manifest(
        input={
            "shape": [1, 2, 2, 3],
            "layout": "NHWC",
            "dtype": "uint8",
            "color_order": "BGR",
            "scale": 1.0,
        }
    )
    image = np.arange(12, dtype=np.uint8).reshape(2, 2, 3)

    tensor, _ = preprocess(image, manifest.input)

    assert tensor.dtype == np.uint8
    assert tensor.shape == (1, 2, 2, 3)
    np.testing.assert_array_equal(tensor[0], image)


def test_letterbox_preserves_aspect_ratio_and_pads_with_configured_value() -> None:
    manifest = make_manifest(input={"shape": [1, 3, 4, 4], "scale": 1.0})
    image = np.full((2, 4, 3), 10, dtype=np.uint8)

    tensor, transform = preprocess(image, manifest.input)

    assert transform.scale_x == transform.scale_y == 1.0
    assert (transform.pad_x, transform.pad_y) == (0, 1)
    channel = tensor[0, 0]
    np.testing.assert_array_equal(channel[0], np.full(4, 114.0))
    np.testing.assert_array_equal(channel[1:3], np.full((2, 4), 10.0))
    np.testing.assert_array_equal(channel[3], np.full(4, 114.0))


def test_stretch_resize_uses_independent_axis_scales() -> None:
    manifest = make_manifest(input={"shape": [1, 3, 4, 4], "resize": "stretch"})

    _, transform = preprocess(np.zeros((2, 8, 3), dtype=np.uint8), manifest.input)

    assert (transform.scale_x, transform.scale_y) == (0.5, 2.0)
    assert (transform.pad_x, transform.pad_y) == (0, 0)


# --------------------------------------------------------------------------- post-processing


def test_yolov8_output_maps_letterboxed_boxes_back_to_source_pixels() -> None:
    manifest = make_manifest(input={"shape": [1, 3, 8, 8]})
    # Source 16x8 letterboxed to 8x8: scale 0.5, pad_y 2.
    output = yolo_v8([[4.0, 4.0, 4.0, 2.0, 0.1, 0.9]])
    session = FakeSession(output)
    detector = OnnxDetector(manifest, session, clock_ns=counter())

    batch = detector.detect(frame(width=16, height=8), context())

    assert len(batch.detections) == 1
    detection = batch.detections[0]
    assert detection.detection_id == "4:0"
    assert (detection.class_id, detection.class_name) == (1, "box")
    assert detection.confidence == pytest.approx(0.9)
    assert detection.bbox_xyxy == pytest.approx((4.0, 2.0, 12.0, 6.0))
    assert session.feeds[0]["images"].shape == (1, 3, 8, 8)


def test_boxes_first_normalized_xyxy_with_objectness_multiplies_scores() -> None:
    manifest = make_manifest(
        output={
            "layout": "boxes_first",
            "box_format": "xyxy",
            "coordinates": "normalized",
            "has_objectness": True,
        }
    )
    output = np.asarray(
        [[[0.25, 0.25, 0.75, 0.5, 0.8, 0.9, 0.2], [0.0, 0.0, 0.5, 0.5, 0.5, 0.9, 0.1]]],
        dtype=np.float32,
    )
    detector = OnnxDetector(manifest, FakeSession(output), clock_ns=counter())

    batch = detector.detect(frame(), context(threshold=0.5))

    assert len(batch.detections) == 1
    assert batch.detections[0].confidence == pytest.approx(0.72)
    assert batch.detections[0].class_name == "bag"
    assert batch.detections[0].bbox_xyxy == pytest.approx((2.0, 2.0, 6.0, 4.0))


def test_confidence_threshold_is_inclusive_and_filters_lower_scores() -> None:
    output = yolo_v8(
        [
            [2.0, 2.0, 2.0, 2.0, 0.5, 0.0],
            [6.0, 6.0, 2.0, 2.0, 0.0, 0.49],
        ]
    )
    detector = OnnxDetector(make_manifest(), FakeSession(output), clock_ns=counter())

    batch = detector.detect(frame(), context(threshold=0.5))

    assert [(item.class_name, item.confidence) for item in batch.detections] == [("bag", 0.5)]


def test_class_aware_nms_suppresses_same_class_overlap_only() -> None:
    output = yolo_v8(
        [
            [4.0, 4.0, 4.0, 4.0, 0.9, 0.0],
            [4.2, 4.0, 4.0, 4.0, 0.8, 0.0],  # same class, IoU > 0.5 -> suppressed
            [4.0, 4.1, 4.0, 4.0, 0.0, 0.7],  # different class -> kept
            [1.0, 1.0, 1.0, 1.0, 0.6, 0.0],  # same class, disjoint -> kept
        ]
    )
    detector = OnnxDetector(make_manifest(), FakeSession(output), clock_ns=counter())

    batch = detector.detect(frame(), context())

    assert [(item.class_name, round(item.confidence, 2)) for item in batch.detections] == [
        ("bag", 0.9),
        ("box", 0.7),
        ("bag", 0.6),
    ]


def test_non_max_suppression_is_greedy_and_deterministic() -> None:
    boxes = np.asarray(
        [[0, 0, 10, 10], [1, 1, 11, 11], [20, 20, 30, 30], [0, 0, 10, 10]], dtype=np.float32
    )
    scores = np.asarray([0.8, 0.9, 0.7, 0.8], dtype=np.float32)

    keep = non_max_suppression(boxes, scores, iou_threshold=0.5)

    assert keep == [1, 2]
    assert non_max_suppression(boxes[:0], scores[:0], 0.5) == []


def test_runtime_and_manifest_class_allow_lists_both_apply() -> None:
    output = yolo_v8([[2.0, 2.0, 2.0, 2.0, 0.9, 0.0], [6.0, 6.0, 2.0, 2.0, 0.0, 0.9]])
    only_bag_runtime = OnnxDetector(make_manifest(), FakeSession(output), clock_ns=counter())
    only_box_manifest = OnnxDetector(
        make_manifest(supported_classes=["box"]), FakeSession(output), clock_ns=counter()
    )

    runtime_batch = only_bag_runtime.detect(frame(), context(classes=("bag",)))
    manifest_batch = only_box_manifest.detect(frame(), context(classes=("bag", "box")))

    assert [item.class_name for item in runtime_batch.detections] == ["bag"]
    assert [item.class_name for item in manifest_batch.detections] == ["box"]


def test_ordering_is_deterministic_for_ties_and_capped_by_max_detections() -> None:
    rows = [
        [6.0, 6.0, 1.0, 1.0, 0.0, 0.8],
        [2.0, 6.0, 1.0, 1.0, 0.8, 0.0],
        [2.0, 2.0, 1.0, 1.0, 0.8, 0.0],
        [6.0, 2.0, 1.0, 1.0, 0.95, 0.0],
    ]
    manifest = make_manifest(defaults={"max_detections": 3})
    detector = OnnxDetector(manifest, FakeSession(yolo_v8(rows)), clock_ns=counter())
    shuffled = OnnxDetector(
        manifest, FakeSession(yolo_v8(list(reversed(rows)))), clock_ns=counter()
    )

    first = detector.detect(frame(sequence=9), context())
    second = shuffled.detect(frame(sequence=9), context())

    expected = [
        ("9:0", "bag", (5.5, 1.5, 6.5, 2.5)),
        ("9:1", "bag", (1.5, 1.5, 2.5, 2.5)),
        ("9:2", "bag", (1.5, 5.5, 2.5, 6.5)),
    ]
    for batch in (first, second):
        observed = [
            (item.detection_id, item.class_name, tuple(round(v, 3) for v in item.bbox_xyxy))
            for item in batch.detections
        ]
        assert observed == expected


def test_boxes_are_clipped_to_frame_and_degenerate_boxes_dropped() -> None:
    output = yolo_v8(
        [
            [7.0, 7.0, 4.0, 4.0, 0.9, 0.0],  # extends past the 8x8 frame -> clipped
            [9.5, 4.0, 1.0, 1.0, 0.0, 0.9],  # entirely outside -> dropped
        ]
    )
    detector = OnnxDetector(make_manifest(), FakeSession(output), clock_ns=counter())

    batch = detector.detect(frame(), context())

    assert len(batch.detections) == 1
    assert batch.detections[0].bbox_xyxy == pytest.approx((5.0, 5.0, 8.0, 8.0))


def test_batch_carries_frame_identity_model_id_and_timing() -> None:
    detector = OnnxDetector(
        make_manifest(), FakeSession(yolo_v8([])), clock_ns=counter(1_000, 250)
    )

    batch = detector.detect(frame(sequence=12), context())

    assert batch.session_id == UUID(int=5)
    assert batch.continuity_segment == 2
    assert batch.frame_sequence == 12
    assert batch.source_timestamp_ms == 1_500
    assert (batch.started_monotonic_ns, batch.completed_monotonic_ns) == (1_000, 1_250)
    assert batch.model_id == "staged-goods"
    assert batch.detections == ()


# --------------------------------------------------------------------------- malformed models


def test_session_factory_failure_is_typed_model_load_failure(tmp_path: Path) -> None:
    manifest_path = write_artifact(tmp_path)

    def broken(path: str) -> Any:
        raise RuntimeError("protobuf parsing failed")

    with pytest.raises(OnnxModelError) as failure:
        OnnxDetector.from_manifest(manifest_path, session_factory=broken)

    assert failure.value.code == "MODEL_LOAD_FAILED"


def test_real_onnxruntime_rejects_corrupt_model_bytes(tmp_path: Path) -> None:
    manifest_path = write_artifact(tmp_path, model_bytes=b"\x00not an onnx graph\xff")

    with pytest.raises(OnnxModelError) as failure:
        OnnxDetector.from_manifest(manifest_path)

    assert failure.value.code == "MODEL_LOAD_FAILED"


@pytest.mark.parametrize(
    ("inputs", "outputs"),
    [
        ([FakeIO("pixels", [1, 3, 8, 8], "tensor(float)")], None),
        ([FakeIO("images", [1, 3, 16, 16], "tensor(float)")], None),
        ([FakeIO("images", [1, 3, 8, 8], "tensor(uint8)")], None),
        ([FakeIO("images", [1, 3, 8, 8], "tensor(float)")] * 2, None),
        (None, [FakeIO("boxes", [1, 6, 3], "tensor(float)")]),
    ],
)
def test_session_signature_must_match_manifest(
    inputs: list[FakeIO] | None, outputs: list[FakeIO] | None
) -> None:
    session = FakeSession(yolo_v8([]), inputs=inputs, outputs=outputs)

    with pytest.raises(OnnxModelError) as failure:
        OnnxDetector(make_manifest(), session)

    assert failure.value.code == "MODEL_SIGNATURE_MISMATCH"


def test_symbolic_batch_dimension_is_accepted() -> None:
    session = FakeSession(
        yolo_v8([]), inputs=[FakeIO("images", ["batch", 3, 8, 8], "tensor(float)")]
    )

    assert OnnxDetector(make_manifest(), session).model_id == "staged-goods"


@pytest.mark.parametrize(
    "output",
    [
        np.zeros((1, 5, 3), dtype=np.float32),  # wrong attribute count
        np.zeros((6, 3), dtype=np.float32),  # missing batch dimension
        np.zeros((2, 6, 3), dtype=np.float32),  # batch of two
        yolo_v8([[1.0, 1.0, 1.0, np.nan, 0.9, 0.0]]),  # non-finite value
        yolo_v8([[1.0, 1.0, 1.0, 1.0, 1.7, 0.0]]),  # score outside [0, 1]
    ],
)
def test_malformed_output_raises_typed_detection_failure(output: np.ndarray) -> None:
    detector = OnnxDetector(make_manifest(), FakeSession(output), clock_ns=counter())

    with pytest.raises(DetectionFailure) as failure:
        detector.detect(frame(sequence=3), context())

    assert failure.value.code == "MODEL_OUTPUT_INVALID"
    assert failure.value.frame_sequence == 3


def test_inference_exception_raises_typed_detection_failure() -> None:
    detector = OnnxDetector(
        make_manifest(), FakeSession(RuntimeError("kernel failed")), clock_ns=counter()
    )

    with pytest.raises(DetectionFailure) as failure:
        detector.detect(frame(sequence=8), context())

    assert failure.value.code == "INFERENCE_FAILED"
    assert failure.value.frame_sequence == 8


# --------------------------------------------------------------------------- real onnxruntime
# A tiny ONNX graph is hand-encoded below (the `onnx` package is not a project dependency):
# one Constant node emits a fixed YOLOv8-shaped [1, 6, 3] tensor while the `images` input
# is declared but unused. This exercises real session loading, signature checks, and run().


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, payload: bytes | int | str) -> bytes:
    if isinstance(payload, int):
        return _varint(number << 3) + _varint(payload)
    data = payload.encode() if isinstance(payload, str) else payload
    return _varint(number << 3 | 2) + _varint(len(data)) + data


def _value_info(name: str, dims: Sequence[int]) -> bytes:
    shape = b"".join(_field(1, _field(1, dim)) for dim in dims)
    tensor_type = _field(1, 1) + _field(2, shape)  # elem_type FLOAT, shape
    return _field(1, name) + _field(2, _field(1, tensor_type))


def tiny_yolo_onnx(values: np.ndarray) -> bytes:
    tensor = b"".join(_field(1, dim) for dim in values.shape)
    tensor += _field(2, 1) + _field(9, values.astype("<f4").tobytes())
    attribute = _field(1, "value") + _field(5, tensor) + _field(20, 4)
    node = _field(2, "output0") + _field(4, "Constant") + _field(5, attribute)
    graph = (
        _field(1, node)
        + _field(2, "tiny-yolo")
        + _field(11, _value_info("images", (1, 3, 8, 8)))
        + _field(12, _value_info("output0", values.shape))
    )
    opset = _field(1, "") + _field(2, 13)
    return _field(1, 8) + _field(2, "pick-zone-tests") + _field(7, graph) + _field(8, opset)


def test_end_to_end_with_real_onnxruntime_and_hand_built_graph(tmp_path: Path) -> None:
    values = yolo_v8(
        [
            [2.0, 2.0, 2.0, 2.0, 0.95, 0.0],
            [2.1, 2.0, 2.0, 2.0, 0.90, 0.0],
            [6.0, 6.0, 2.0, 2.0, 0.0, 0.30],
        ]
    )
    manifest_path = write_artifact(tmp_path, model_bytes=tiny_yolo_onnx(values))

    detector = OnnxDetector.from_manifest(manifest_path, clock_ns=counter())
    batch = detector.detect(frame(), context(threshold=0.5))

    assert [(item.class_name, round(item.confidence, 2)) for item in batch.detections] == [
        ("bag", 0.95)
    ]
    assert batch.detections[0].bbox_xyxy == pytest.approx((1.0, 1.0, 3.0, 3.0))

"""Model manifest schema plus the SHA-256 and license gate (plan section 7.1).

The application refuses a model whose manifest or file is missing, whose file hash does
not match, whose path escapes the manifest directory, or whose license is unknown or
does not allow commercial use. No model download ever happens at runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_HASH_CHUNK_BYTES = 1 << 20
_UNKNOWN_LICENSE_NAMES = frozenset({"", "unknown", "none", "unlicensed", "noassertion"})
_NON_COMMERCIAL_LICENSE = re.compile(r"(^|[-_ ])nc([-_ ]|$)|non[-_ ]?commercial", re.I)


class ModelManifestError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelLicense(_Frozen):
    name: str
    source: str
    commercial_use_allowed: bool


class ModelInputSpec(_Frozen):
    name: str = Field(min_length=1)
    shape: tuple[int, int, int, int]
    layout: Literal["NCHW", "NHWC"]
    dtype: Literal["float32", "uint8"]
    color_order: Literal["RGB", "BGR"] = "RGB"
    scale: float = Field(default=1 / 255, gt=0)
    mean: tuple[float, float, float] = (0.0, 0.0, 0.0)
    std: tuple[float, float, float] = (1.0, 1.0, 1.0)
    resize: Literal["letterbox", "stretch"] = "letterbox"
    pad_value: int = Field(default=114, ge=0, le=255)

    @model_validator(mode="after")
    def validate_tensor_layout(self) -> Self:
        batch, *rest = self.shape
        channels = rest[0] if self.layout == "NCHW" else rest[2]
        height, width = (rest[1], rest[2]) if self.layout == "NCHW" else (rest[0], rest[1])
        if batch != 1:
            raise ValueError("input batch dimension must be 1")
        if channels != 3:
            raise ValueError(f"input shape does not have 3 channels in {self.layout} layout")
        if height <= 0 or width <= 0:
            raise ValueError("input height and width must be positive")
        if any(value <= 0 for value in self.std):
            raise ValueError("normalization std values must be positive")
        if self.dtype == "uint8" and (
            self.scale != 1.0 or self.mean != (0.0, 0.0, 0.0) or self.std != (1.0, 1.0, 1.0)
        ):
            raise ValueError("uint8 inputs cannot declare scale/mean/std normalization")
        return self

    @property
    def height(self) -> int:
        return self.shape[2] if self.layout == "NCHW" else self.shape[1]

    @property
    def width(self) -> int:
        return self.shape[3] if self.layout == "NCHW" else self.shape[2]


class ModelOutputSpec(_Frozen):
    """YOLO-shaped output mapping.

    `attributes_first` is `[1, A, N]` (YOLOv8 export); `boxes_first` is `[1, N, A]`
    (YOLOv5 export). Each candidate's attributes are the four box values, an optional
    objectness score, then one score per class in class-ID order.
    """

    name: str = Field(min_length=1)
    layout: Literal["attributes_first", "boxes_first"]
    box_format: Literal["cxcywh", "xyxy"]
    coordinates: Literal["input_pixels", "normalized"] = "input_pixels"
    has_objectness: bool = False


class ModelClass(_Frozen):
    id: int = Field(ge=0)
    name: str = Field(min_length=1)


class ModelDefaults(_Frozen):
    confidence_threshold: float = Field(ge=0, le=1)
    nms_iou_threshold: float = Field(gt=0, le=1)
    max_detections: int = Field(ge=1, le=1_000)


class ModelValidationInfo(_Frozen):
    clip_set: str
    metrics: dict[str, float] = Field(default_factory=dict)


class ModelManifest(_Frozen):
    schema_version: Literal[1]
    model_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_file: str = Field(min_length=1)
    source: str = Field(min_length=1)
    license: ModelLicense
    input: ModelInputSpec
    output: ModelOutputSpec
    classes: tuple[ModelClass, ...] = Field(min_length=1)
    supported_classes: tuple[str, ...] = Field(min_length=1)
    defaults: ModelDefaults
    validation: ModelValidationInfo | None = None

    @model_validator(mode="after")
    def validate_classes(self) -> Self:
        ids = [item.id for item in self.classes]
        if ids != list(range(len(ids))):
            raise ValueError("class IDs must be unique, ordered, and contiguous from 0")
        names = [item.name for item in self.classes]
        if len(set(names)) != len(names):
            raise ValueError("class names must be unique")
        unknown = set(self.supported_classes) - set(names)
        if unknown:
            raise ValueError(f"supported classes are not model classes: {sorted(unknown)}")
        return self

    @property
    def class_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.classes)

    @property
    def attribute_count(self) -> int:
        return 4 + int(self.output.has_objectness) + len(self.classes)


@dataclass(frozen=True, slots=True)
class VerifiedModel:
    manifest: ModelManifest
    model_path: Path
    sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(manifest_path: Path) -> ModelManifest:
    if not manifest_path.is_file():
        raise ModelManifestError("MANIFEST_MISSING", manifest_path.name)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        return ModelManifest.model_validate(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise ModelManifestError("MANIFEST_INVALID", str(error)) from error


def check_license(license_info: ModelLicense) -> None:
    name = license_info.name.strip()
    if name.lower() in _UNKNOWN_LICENSE_NAMES:
        raise ModelManifestError("MODEL_LICENSE_REJECTED", "license is unknown")
    if not license_info.source.strip():
        raise ModelManifestError("MODEL_LICENSE_REJECTED", "license source is not recorded")
    if not license_info.commercial_use_allowed or _NON_COMMERCIAL_LICENSE.search(name):
        raise ModelManifestError("MODEL_LICENSE_REJECTED", f"{name} is non-commercial")


def resolve_model_path(manifest_path: Path, model_file: str) -> Path:
    base = manifest_path.resolve().parent
    if PurePosixPath(model_file).is_absolute() or PureWindowsPath(model_file).is_absolute():
        raise ModelManifestError("MODEL_PATH_NOT_ALLOWED", "model_file must be relative")
    candidate = (base / model_file).resolve()
    if not candidate.is_relative_to(base):
        raise ModelManifestError("MODEL_PATH_NOT_ALLOWED", "model_file escapes manifest directory")
    return candidate


def verify_model_artifact(manifest_path: Path) -> VerifiedModel:
    """Run every gate in order: manifest, license, path containment, presence, hash."""

    manifest = load_manifest(manifest_path)
    check_license(manifest.license)
    model_path = resolve_model_path(manifest_path, manifest.model_file)
    if not model_path.is_file():
        raise ModelManifestError("MODEL_FILE_MISSING", manifest.model_file)
    actual = sha256_file(model_path)
    if actual != manifest.sha256:
        raise ModelManifestError(
            "MODEL_HASH_MISMATCH", f"{manifest.model_file}: expected {manifest.sha256}"
        )
    return VerifiedModel(manifest=manifest, model_path=model_path, sha256=actual)

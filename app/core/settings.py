from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator


def _default_project_dir() -> Path:
    return Path(__file__).resolve().parents[2]


class Settings(BaseModel):
    """Validated local runtime settings for the demo."""

    project_dir: Path = Field(default_factory=_default_project_dir)
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8000, ge=1, le=65535)
    analysis_fps: int = Field(default=4, ge=3, le=5)
    data_dir: Path | None = None
    upload_dir: Path | None = None
    evidence_dir: Path | None = None
    model_dir: Path | None = None

    @model_validator(mode="after")
    def resolve_and_validate_paths(self) -> Self:
        project_dir = self.project_dir.expanduser().resolve()
        object.__setattr__(self, "project_dir", project_dir)

        resolved_paths = {
            "data_dir": self.data_dir or project_dir / "data",
            "upload_dir": self.upload_dir or project_dir / "data" / "uploads",
            "evidence_dir": self.evidence_dir or project_dir / "data" / "evidence",
            "model_dir": self.model_dir or project_dir / "models",
        }
        for field_name, raw_path in resolved_paths.items():
            path = raw_path.expanduser().resolve()
            if not path.is_relative_to(project_dir):
                raise ValueError(f"{field_name} must be inside project_dir")
            object.__setattr__(self, field_name, path)
        return self

    @classmethod
    def from_env(cls) -> Self:
        mapping: dict[str, tuple[str, type[str] | type[int]]] = {
            "project_dir": ("PICK_ZONE_PROJECT_DIR", str),
            "bind_host": ("PICK_ZONE_BIND_HOST", str),
            "bind_port": ("PICK_ZONE_BIND_PORT", int),
            "analysis_fps": ("PICK_ZONE_ANALYSIS_FPS", int),
            "data_dir": ("PICK_ZONE_DATA_DIR", str),
            "upload_dir": ("PICK_ZONE_UPLOAD_DIR", str),
            "evidence_dir": ("PICK_ZONE_EVIDENCE_DIR", str),
            "model_dir": ("PICK_ZONE_MODEL_DIR", str),
        }
        values: dict[str, Any] = {}
        for field_name, (environment_name, converter) in mapping.items():
            raw_value = os.getenv(environment_name)
            if raw_value is not None:
                values[field_name] = converter(raw_value)
        return cls(**values)

    def prepare_directories(self) -> None:
        for path in (self.data_dir, self.upload_dir, self.evidence_dir, self.model_dir):
            assert path is not None
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-probe"
            try:
                probe.write_bytes(b"")
            finally:
                probe.unlink(missing_ok=True)

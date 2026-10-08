from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.settings import Settings

EXPECTED_DISTRIBUTIONS = {
    "fastapi": "0.143.0",
    "uvicorn": "0.54.0",
    "opencv-python": "5.0.0.93",
    "onnxruntime": "1.30.0",
    "openpyxl": "3.1.5",
    "pydantic": "2.13.5",
    "numpy": "2.5.3",
    "python-multipart": "0.0.32",
    "pytest": "9.1.1",
    "pytest-asyncio": "1.4.0",
    "httpx": "0.28.1",
    "playwright": "1.63.0",
    "ruff": "0.16.10",
}


def test_runtime_and_direct_dependency_versions_are_locked() -> None:
    assert sys.version_info[:2] == (3, 12)
    installed = {name: importlib.metadata.version(name) for name in EXPECTED_DISTRIBUTIONS}
    assert installed == EXPECTED_DISTRIBUTIONS


def test_settings_default_to_loopback_and_four_fps(tmp_path: Path) -> None:
    settings = Settings(project_dir=tmp_path)

    assert settings.bind_host == "127.0.0.1"
    assert settings.bind_port == 8000
    assert settings.analysis_fps == 4
    assert settings.data_dir == tmp_path / "data"
    assert settings.upload_dir == tmp_path / "data" / "uploads"
    assert settings.evidence_dir == tmp_path / "data" / "evidence"
    assert settings.model_dir == tmp_path / "models"


@pytest.mark.parametrize("analysis_fps", [3, 4, 5])
def test_settings_accept_supported_analysis_rates(tmp_path: Path, analysis_fps: int) -> None:
    assert Settings(project_dir=tmp_path, analysis_fps=analysis_fps).analysis_fps == analysis_fps


@pytest.mark.parametrize("analysis_fps", [0, 2, 6, 30])
def test_settings_reject_unsupported_analysis_rates(tmp_path: Path, analysis_fps: int) -> None:
    with pytest.raises(ValidationError):
        Settings(project_dir=tmp_path, analysis_fps=analysis_fps)


def test_runtime_paths_cannot_escape_project_directory(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="inside project_dir"):
        Settings(project_dir=tmp_path, data_dir=tmp_path.parent / "outside")


def test_prepare_directories_creates_writable_runtime_paths(tmp_path: Path) -> None:
    settings = Settings(project_dir=tmp_path)

    settings.prepare_directories()

    assert settings.data_dir.is_dir()
    assert settings.upload_dir.is_dir()
    assert settings.evidence_dir.is_dir()
    assert settings.model_dir.is_dir()


def test_settings_read_namespaced_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PICK_ZONE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("PICK_ZONE_BIND_PORT", "8123")
    monkeypatch.setenv("PICK_ZONE_ANALYSIS_FPS", "5")

    settings = Settings.from_env()

    assert settings.project_dir == tmp_path
    assert settings.bind_port == 8123
    assert settings.analysis_fps == 5

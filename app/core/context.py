"""Application component graph: settings, workbook persistence, runtime state, factories.

The context owns process-wide singletons (one workbook writer thread, one persistence
channel, one runtime state store). Per-session components are built by the lifecycle service
through the injected factories so tests and later tasks can swap sources, detectors, evidence,
and scenario restoration without touching the lifecycle.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.core.settings import Settings
from app.persistence.schema import WorkbookFailure
from app.persistence.workbook import WorkbookRepository, WorkbookSnapshot
from app.persistence.writer import CommitReceipt, WorkbookWriter, WorkbookWriterWorker
from app.services.orchestrator import PERSISTENCE_CAPACITY, PersistenceChannel
from app.services.runtime_state import Alert, RuntimeStateStore
from app.video.evidence import EvidenceManager
from app.video.network_source import NetworkVideoSource
from app.video.sources import CameraVideoSource, FileVideoSource, VideoSource
from app.vision.detector import Detector
from app.vision.onnx_detector import OnnxDetector

Row = Mapping[str, Any]
DEFAULT_WORKBOOK_NAME = "pick-zone-demo.xlsx"
MODEL_MANIFEST_NAME = "model-manifest.json"
FFMPEG = "ffmpeg"

SourceFactory = Callable[[Row, str | None], VideoSource]
DetectorFactory = Callable[[Row, frozenset[str]], Detector]
EvidenceFactory = Callable[[UUID], Any]
ScenarioRestorer = Callable[[str], None]
DetectorCheck = Callable[[], str | None]


class CommitWriter(Protocol):
    def commit(self, mutation: Any) -> CommitReceipt: ...


class DetectorUnavailable(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    status: str  # "ready" | "degraded"
    problems: tuple[str, ...]


def _inside(base: Path, relative: str) -> Path:
    path = (base / relative).expanduser().resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError("source file must be inside the data directory")
    return path


def default_source_factory(settings: Settings) -> SourceFactory:
    """Build the production source for a SourceProfiles row."""
    assert settings.data_dir is not None
    data_dir = settings.data_dir

    def factory(profile: Row, network_url_override: str | None) -> VideoSource:
        source_type = str(profile["source_type"])
        if source_type in {"file", "replay"}:
            return FileVideoSource(_inside(data_dir, str(profile["file_path"])))
        if source_type == "camera":
            return CameraVideoSource(
                int(profile["camera_index"]),
                requested_width=int(profile.get("requested_width") or 1280),
                requested_height=int(profile.get("requested_height") or 720),
            )
        if source_type == "network":
            url = network_url_override or str(profile["network_url_redacted"])
            return NetworkVideoSource(url, reconnect_enabled=bool(profile["reconnect_enabled"]))
        raise ValueError(f"unsupported source type {source_type}")

    return factory


def default_detector_check(settings: Settings) -> DetectorCheck:
    assert settings.model_dir is not None
    manifest = settings.model_dir / MODEL_MANIFEST_NAME

    def check() -> str | None:
        return None if manifest.is_file() else "DETECTOR_UNAVAILABLE"

    return check


def default_detector_factory(settings: Settings) -> DetectorFactory:
    """ONNX detector from ``model_dir``; replay fixtures are wired by later tasks."""
    assert settings.model_dir is not None
    manifest = settings.model_dir / MODEL_MANIFEST_NAME

    def factory(profile: Row, allowed_class_names: frozenset[str]) -> Detector:
        if str(profile["source_type"]) == "replay":
            raise DetectorUnavailable("DETECTOR_UNAVAILABLE", "replay fixtures are not configured")
        if not manifest.is_file():
            raise DetectorUnavailable("DETECTOR_UNAVAILABLE", "model manifest missing")
        return OnnxDetector.from_manifest(manifest)

    return factory


def default_evidence_factory(
    settings: Settings, which: Callable[[str], str | None]
) -> EvidenceFactory:
    assert settings.evidence_dir is not None
    evidence_dir = settings.evidence_dir

    def factory(session_id: UUID) -> EvidenceManager | None:
        if which(FFMPEG) is None:
            return None
        return EvidenceManager(evidence_dir, session_id, which=which)

    return factory


class ApplicationContext:
    def __init__(
        self,
        *,
        settings: Settings,
        workbook_path: Path,
        repository: WorkbookRepository,
        writer_worker: WorkbookWriterWorker,
        persistence: PersistenceChannel,
        runtime: RuntimeStateStore,
        source_factory: SourceFactory,
        detector_factory: DetectorFactory,
        evidence_factory: EvidenceFactory,
        scenario_restorer: ScenarioRestorer | None,
        detector_check: DetectorCheck,
        which: Callable[[str], str | None],
        now: Callable[[], datetime],
        monotonic_ns: Callable[[], int],
        uuid_factory: Callable[[], UUID],
    ) -> None:
        self.settings = settings
        self.workbook_path = workbook_path
        self.repository = repository
        self.writer_worker = writer_worker
        self.persistence = persistence
        self.runtime = runtime
        self.source_factory = source_factory
        self.detector_factory = detector_factory
        self.evidence_factory = evidence_factory
        self.scenario_restorer = scenario_restorer
        self.detector_check = detector_check
        self.which = which
        self.now = now
        self.monotonic_ns = monotonic_ns
        self.uuid_factory = uuid_factory
        self._started = False
        persistence.add_listener(self._publish_persistence)

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        workbook_path: Path | None = None,
        writer: CommitWriter | None = None,
        source_factory: SourceFactory | None = None,
        detector_factory: DetectorFactory | None = None,
        evidence_factory: EvidenceFactory | None = None,
        scenario_restorer: ScenarioRestorer | None = None,
        detector_check: DetectorCheck | None = None,
        which: Callable[[str], str | None] = shutil.which,
        now: Callable[[], datetime] | None = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        uuid_factory: Callable[[], UUID] = uuid4,
        writer_capacity: int = PERSISTENCE_CAPACITY,
    ) -> ApplicationContext:
        assert settings.data_dir is not None
        clock = now or (lambda: datetime.now(UTC))
        path = (workbook_path or settings.data_dir / DEFAULT_WORKBOOK_NAME).expanduser().resolve()
        repository = WorkbookRepository(settings.data_dir)
        commit_writer = writer or WorkbookWriter(repository, path, now=clock)
        worker = WorkbookWriterWorker(commit_writer, capacity=writer_capacity)  # type: ignore[arg-type]

        def reload_version() -> int:
            return repository.load_snapshot(path).workbook_version

        persistence = PersistenceChannel(
            worker, capacity=writer_capacity, reload_version=reload_version, now=clock
        )
        return cls(
            settings=settings,
            workbook_path=path,
            repository=repository,
            writer_worker=worker,
            persistence=persistence,
            runtime=RuntimeStateStore(now=clock),
            source_factory=source_factory or default_source_factory(settings),
            detector_factory=detector_factory or default_detector_factory(settings),
            evidence_factory=evidence_factory or default_evidence_factory(settings, which),
            scenario_restorer=scenario_restorer,
            detector_check=detector_check or default_detector_check(settings),
            which=which,
            now=clock,
            monotonic_ns=monotonic_ns,
            uuid_factory=uuid_factory,
        )

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the writer thread and sync persistence state with the workbook on disk."""
        if self._started:
            return
        self.writer_worker.start()
        self._started = True
        self.refresh_persistence()

    def refresh_persistence(self) -> WorkbookSnapshot | None:
        """Reload the workbook; ready at its version if it loads, blocked otherwise."""
        try:
            snapshot = self.load_snapshot()
        except WorkbookFailure as failure:
            self.persistence.mark_blocked(failure.code)
            return None
        self.persistence.recover(snapshot.workbook_version)
        return snapshot

    def close(self, timeout_s: float = 5.0) -> bool:
        """Flush committed work and stop the writer thread; ``False`` if it timed out."""
        deadline = time.monotonic() + timeout_s
        flushed = self.persistence.drain(timeout_s)
        self.persistence.close()
        if not self._started:
            return flushed
        self._started = False
        try:
            self.writer_worker.stop(max(0.0, deadline - time.monotonic()))
        except TimeoutError:
            return False
        return flushed

    def load_snapshot(self) -> WorkbookSnapshot:
        return self.repository.load_snapshot(self.workbook_path)

    # ------------------------------------------------------------------ health

    def readiness(self) -> ReadinessReport:
        problems: list[str] = []
        try:
            self.load_snapshot()
        except WorkbookFailure as failure:
            problems.append(failure.code)
        for path in (self.settings.data_dir, self.settings.upload_dir, self.settings.evidence_dir):
            if path is None or not self._writable(path):
                problems.append("STORAGE_UNAVAILABLE")
                break
        if self.which(FFMPEG) is None:
            problems.append("EVIDENCE_UNAVAILABLE")
        detector_problem = self.detector_check()
        if detector_problem:
            problems.append(detector_problem)
        if self.persistence.state != "ready":
            problems.append("PERSISTENCE_BLOCKED")
        unique = tuple(dict.fromkeys(problems))
        return ReadinessReport("ready" if not unique else "degraded", unique)

    @staticmethod
    def _writable(path: Path) -> bool:
        probe = path / ".write-probe"
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe.write_bytes(b"")
            probe.unlink()
        except OSError:
            return False
        return True

    def _publish_persistence(self, channel: PersistenceChannel) -> None:
        self.runtime.update_persistence(state=channel.state, last_commit_at=channel.last_commit_at)
        if channel.state == "ready":
            self.runtime.clear_alert("PERSISTENCE_BLOCKED")
        else:
            self.runtime.raise_alert(
                Alert(
                    "PERSISTENCE_BLOCKED",
                    "error",
                    f"Workbook {channel.state} ({channel.error_code}); auto-approval is disabled.",
                )
            )

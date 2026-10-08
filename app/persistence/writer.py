from __future__ import annotations

import json
import os
import queue
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from app.domain.mutations import WorkbookMutation
from app.persistence.schema import PRIMARY_KEYS, SHEET_HEADERS, WorkbookFailure
from app.persistence.workbook import Row, WorkbookRepository, WorkbookSnapshot


@dataclass(frozen=True, slots=True)
class CommitReceipt:
    mutation_id: str
    idempotency_key: str
    event_id: str
    mutation_hash: str
    committed_at: str
    workbook_version_before: int
    workbook_version_after: int

    def to_dict(self) -> dict[str, object]:
        return {
            "mutation_id": self.mutation_id,
            "idempotency_key": self.idempotency_key,
            "event_id": self.event_id,
            "mutation_hash": self.mutation_hash,
            "committed_at": self.committed_at,
            "workbook_version_before": self.workbook_version_before,
            "workbook_version_after": self.workbook_version_after,
        }


class WorkbookWriter:
    def __init__(
        self,
        repository: WorkbookRepository,
        path: Path,
        *,
        replace_file: Callable[[Path, Path], None] = os.replace,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._path = path.expanduser().resolve()
        self._replace_file = replace_file
        self._now = now
        self._lock = threading.Lock()
        self._snapshot: WorkbookSnapshot | None = None

    def commit(self, mutation: WorkbookMutation) -> CommitReceipt:
        with self._lock:
            snapshot = self._repository.load_snapshot(self._path)
            duplicate = snapshot.indexes.receipt_by_idempotency_key.get(mutation.idempotency_key)
            if duplicate:
                if duplicate["mutation_hash"] != mutation.canonical_effects_hash:
                    raise WorkbookFailure("IDEMPOTENCY_CONFLICT", mutation.idempotency_key)
                return self._receipt_from_row(duplicate)

            if mutation.expected_workbook_version != snapshot.workbook_version:
                raise WorkbookFailure(
                    "STALE_SNAPSHOT",
                    "expected "
                    f"{mutation.expected_workbook_version}, actual {snapshot.workbook_version}",
                )
            self._validate_preconditions(snapshot, mutation)
            rows = {
                sheet: [dict(item) for item in sheet_rows]
                for sheet, sheet_rows in snapshot.rows.items()
            }
            self._apply_mutation(rows, mutation)
            self._reject_negative_inventory(rows)

            committed_at = self._now().isoformat()
            receipt = self._build_receipt(snapshot, mutation, committed_at)
            rows["MutationReceipts"].append(
                {
                    **receipt.to_dict(),
                    "result_json": json.dumps(
                        receipt.to_dict(), sort_keys=True, separators=(",", ":")
                    ),
                }
            )
            self._update_metadata(rows, mutation, committed_at)
            frozen_rows = {sheet: tuple(items) for sheet, items in rows.items()}
            report = self._repository.validate_rows(frozen_rows)
            if not report.valid:
                code, _, detail = report.errors[0].partition(":")
                raise WorkbookFailure(code, detail.strip())

            temporary = self._path.parent / (f".{self._path.stem}.{mutation.mutation_id}.tmp.xlsx")
            try:
                self._save_and_sync(temporary, rows)
                verified = self._repository.load_snapshot(temporary)
                stored = verified.indexes.receipt_by_idempotency_key.get(mutation.idempotency_key)
                if not stored or stored["mutation_hash"] != mutation.canonical_effects_hash:
                    raise WorkbookFailure(
                        "POST_WRITE_VERIFY_FAILED", "commit receipt missing from temporary workbook"
                    )
                backup = self._path.with_suffix(".bak.xlsx")
                shutil.copy2(self._path, backup)
                try:
                    self._replace_file(temporary, self._path)
                except OSError as error:
                    raise WorkbookFailure("ATOMIC_REPLACE_FAILED", str(error)) from error
                self._fsync_directory(self._path.parent)
                self._snapshot = self._repository.load_snapshot(self._path)
                return receipt
            except WorkbookFailure:
                raise
            except OSError as error:
                raise WorkbookFailure("TEMP_SAVE_FAILED", str(error)) from error
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_preconditions(snapshot: WorkbookSnapshot, mutation: WorkbookMutation) -> None:
        entity_maps: dict[str, tuple[str, str]] = {
            "task": ("Tasks", "task_id"),
            "inventory": ("Inventory", "inventory_id"),
            "event": ("Events", "event_id"),
        }
        for precondition in mutation.preconditions:
            if precondition.entity not in entity_maps:
                raise WorkbookFailure(
                    "STALE_SNAPSHOT", f"unknown precondition entity {precondition.entity}"
                )
            sheet, key_name = entity_maps[precondition.entity]
            current = next(
                (
                    item
                    for item in snapshot.rows[sheet]
                    if str(item[key_name]) == precondition.entity_id
                ),
                None,
            )
            for field, expected in precondition.expected.items():
                actual = current.get(field) if current else None
                if actual != expected:
                    raise WorkbookFailure(
                        "STALE_SNAPSHOT",
                        f"{precondition.entity}:{precondition.entity_id}.{field}",
                    )

    @staticmethod
    def _apply_mutation(rows: dict[str, list[Row]], mutation: WorkbookMutation) -> None:
        if mutation.deletes:
            raise WorkbookFailure("ROW_VALIDATION_ERROR", "deletes are reserved for reset")
        for sheet, incoming_rows in mutation.upserts.items():
            if sheet not in SHEET_HEADERS:
                raise WorkbookFailure("SHEET_MISSING", sheet)
            primary_key = PRIMARY_KEYS[sheet]
            indexed = {str(item[primary_key]): item for item in rows[sheet]}
            for incoming in incoming_rows:
                key = str(incoming.get(primary_key) or "")
                if not key:
                    raise WorkbookFailure("ROW_VALIDATION_ERROR", f"{sheet}.{primary_key} required")
                if key in indexed:
                    indexed[key].update(incoming)
                else:
                    created = {header: "" for header in SHEET_HEADERS[sheet]}
                    created.update(incoming)
                    rows[sheet].append(created)
                    indexed[key] = created
        for sheet, incoming_rows in mutation.appends.items():
            if sheet == "MutationReceipts":
                continue
            if sheet not in SHEET_HEADERS:
                raise WorkbookFailure("SHEET_MISSING", sheet)
            for incoming in incoming_rows:
                created = {header: "" for header in SHEET_HEADERS[sheet]}
                created.update(incoming)
                rows[sheet].append(created)

    @staticmethod
    def _reject_negative_inventory(rows: dict[str, list[Row]]) -> None:
        if any(
            isinstance(item.get("quantity"), int) and item["quantity"] < 0
            for item in rows["Inventory"]
        ):
            raise WorkbookFailure(
                "INSUFFICIENT_INVENTORY", "inventory quantity would become negative"
            )

    @staticmethod
    def _build_receipt(
        snapshot: WorkbookSnapshot,
        mutation: WorkbookMutation,
        committed_at: str,
    ) -> CommitReceipt:
        event_rows = mutation.upserts.get("Events", ())
        event_id = str(event_rows[0].get("event_id", "")) if event_rows else ""
        return CommitReceipt(
            mutation_id=str(mutation.mutation_id),
            idempotency_key=mutation.idempotency_key,
            event_id=event_id,
            mutation_hash=mutation.canonical_effects_hash,
            committed_at=committed_at,
            workbook_version_before=snapshot.workbook_version,
            workbook_version_after=snapshot.workbook_version + 1,
        )

    @staticmethod
    def _update_metadata(
        rows: dict[str, list[Row]], mutation: WorkbookMutation, committed_at: str
    ) -> None:
        metadata = {str(item["key"]): item for item in rows["WorkbookMeta"]}
        metadata["last_committed_at"]["value"] = committed_at
        metadata["last_committed_at"]["updated_at"] = committed_at
        metadata["last_mutation_id"]["value"] = str(mutation.mutation_id)
        metadata["last_mutation_id"]["updated_at"] = committed_at

    @staticmethod
    def _save_and_sync(path: Path, rows: dict[str, list[Row]]) -> None:
        workbook = Workbook()
        workbook.remove(workbook.active)
        for sheet_name, headers in SHEET_HEADERS.items():
            sheet = workbook.create_sheet(sheet_name)
            sheet.append(headers)
            for item in rows[sheet_name]:
                sheet.append(
                    [WorkbookWriter._cell_value(item.get(header, "")) for header in headers]
                )
        workbook.save(path)
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

    @staticmethod
    def _cell_value(value: Any) -> Any:
        if isinstance(value, (dict, list, tuple, set, frozenset)):
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        return value

    @staticmethod
    def _receipt_from_row(row: Row) -> CommitReceipt:
        return CommitReceipt(
            mutation_id=str(row["mutation_id"]),
            idempotency_key=str(row["idempotency_key"]),
            event_id=str(row["event_id"]),
            mutation_hash=str(row["mutation_hash"]),
            committed_at=str(row["committed_at"]),
            workbook_version_before=int(row["workbook_version_before"]),
            workbook_version_after=int(row["workbook_version_after"]),
        )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


_STOP = object()


class WorkbookWriterWorker:
    def __init__(self, writer: WorkbookWriter, capacity: int = 100) -> None:
        if capacity <= 0:
            raise ValueError("writer queue capacity must be positive")
        self._writer = writer
        self._queue: queue.Queue[tuple[WorkbookMutation, Future[CommitReceipt]] | object] = (
            queue.Queue(maxsize=capacity)
        )
        self._accepting = False
        self._thread: threading.Thread | None = None

    @property
    def thread_name(self) -> str:
        return "pick-zone-workbook-writer"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._accepting = True
        self._thread = threading.Thread(target=self._run, name=self.thread_name, daemon=False)
        self._thread.start()

    def submit(self, mutation: WorkbookMutation) -> Future[CommitReceipt]:
        if not self._accepting:
            raise RuntimeError("workbook writer is stopped")
        future: Future[CommitReceipt] = Future()
        try:
            self._queue.put_nowait((mutation, future))
        except queue.Full as error:
            raise WorkbookFailure("PERSISTENCE_BLOCKED", "writer queue is full") from error
        return future

    def stop(self, timeout_seconds: float = 5) -> None:
        self._accepting = False
        if self._thread is None:
            return
        self._queue.put(_STOP)
        self._thread.join(timeout_seconds)
        if self._thread.is_alive():
            raise TimeoutError("workbook writer did not stop before timeout")

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                mutation, future = item
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(self._writer.commit(mutation))
                    except BaseException as error:
                        future.set_exception(error)
            finally:
                self._queue.task_done()

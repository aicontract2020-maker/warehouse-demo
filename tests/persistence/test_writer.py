from __future__ import annotations

import json
import threading
from pathlib import Path
from uuid import UUID

import pytest

from app.domain.mutations import MutationPrecondition, WorkbookMutation
from app.persistence.schema import WorkbookFailure
from app.persistence.workbook import WorkbookRepository
from app.persistence.writer import WorkbookWriter, WorkbookWriterWorker
from tests.persistence.test_workbook import write_workbook


def mutation(
    *,
    mutation_id: int = 100,
    idempotency_key: str = "event-100",
    effects_hash: str = "a" * 64,
    expected_workbook_version: int = 0,
    source_quantity: int = 8,
) -> WorkbookMutation:
    event_id = "00000000-0000-0000-0000-000000000100"
    return WorkbookMutation(
        mutation_id=UUID(int=mutation_id),
        idempotency_key=idempotency_key,
        request_id=None,
        expected_workbook_version=expected_workbook_version,
        preconditions=(
            MutationPrecondition("task", "task-1", {"status": "open"}),
            MutationPrecondition("inventory", "inventory-source", {"version": 1, "quantity": 10}),
            MutationPrecondition("event", event_id, {"mutation_id": None}),
        ),
        upserts={
            "Inventory": (
                {
                    "inventory_id": "inventory-source",
                    "quantity": source_quantity,
                    "version": 2,
                    "last_event_id": event_id,
                },
                {
                    "inventory_id": "inventory-destination",
                    "quantity": 2,
                    "version": 2,
                    "last_event_id": event_id,
                },
            ),
            "Tasks": (
                {
                    "task_id": "task-1",
                    "status": "completed",
                    "selected": False,
                    "completed_event_id": event_id,
                },
            ),
            "Events": (
                {
                    "event_id": event_id,
                    "task_id": "task-1",
                    "zone_profile_id": "zone-1",
                    "zone_version": 1,
                    "state": "approved",
                    "observed_sku_id": "sku-box",
                    "observed_quantity": 2,
                    "observed_unit": "case",
                    "observed_source_location_id": "A-03-01",
                    "final_sku_id": "sku-box",
                    "final_quantity": 2,
                    "final_unit": "case",
                    "final_source_location_id": "A-03-01",
                    "aggregate_confidence": 0.999,
                    "integrity_flags_json": "[]",
                    "decision": "auto_approved",
                    "started_at": "2026-10-08T16:00:00+00:00",
                    "ended_at": "2026-10-08T16:00:05+00:00",
                    "created_at": "2026-10-08T16:00:00+00:00",
                    "decided_at": "2026-10-08T16:00:05+00:00",
                    "applied_at": "2026-10-08T16:00:05+00:00",
                    "mutation_id": str(UUID(int=mutation_id)),
                },
            ),
        },
        appends={
            "AuditLog": (
                {
                    "audit_id": f"audit-{mutation_id}",
                    "timestamp": "2026-10-08T16:00:05+00:00",
                    "operator_id": "system",
                    "action": "auto_approved",
                    "entity_type": "event",
                    "entity_id": event_id,
                    "result": "success",
                },
            ),
            "MutationReceipts": (
                {
                    "mutation_id": str(UUID(int=mutation_id)),
                    "idempotency_key": idempotency_key,
                    "event_id": event_id,
                },
            ),
        },
        deletes={},
        canonical_effects_hash=effects_hash,
    )


def test_commit_persists_all_effects_together_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.xlsx"
    write_workbook(path)
    repository = WorkbookRepository(tmp_path)
    writer = WorkbookWriter(repository, path)

    receipt = writer.commit(mutation())
    reopened = repository.load_snapshot(path)

    assert receipt.workbook_version_before == 0
    assert receipt.workbook_version_after == 1
    assert reopened.workbook_version == 1
    assert reopened.indexes.inventory_by_location_sku[("A-03-01", "sku-box")]["quantity"] == 8
    assert reopened.indexes.inventory_by_location_sku[("STAGING", "sku-box")]["quantity"] == 2
    assert reopened.indexes.task_by_id["task-1"]["status"] == "completed"
    assert (
        reopened.indexes.event_by_id["00000000-0000-0000-0000-000000000100"]["decision"]
        == "auto_approved"
    )
    assert path.with_suffix(".bak.xlsx").is_file()


def test_duplicate_same_hash_returns_original_receipt_without_second_apply(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.xlsx"
    write_workbook(path)
    writer = WorkbookWriter(WorkbookRepository(tmp_path), path)

    first = writer.commit(mutation())
    second = writer.commit(mutation(mutation_id=999))

    assert second == first
    assert WorkbookRepository(tmp_path).load_snapshot(path).workbook_version == 1


def test_duplicate_key_with_different_hash_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.xlsx"
    write_workbook(path)
    writer = WorkbookWriter(WorkbookRepository(tmp_path), path)
    writer.commit(mutation())

    with pytest.raises(WorkbookFailure) as conflict:
        writer.commit(mutation(mutation_id=101, effects_hash="b" * 64))

    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    assert WorkbookRepository(tmp_path).load_snapshot(path).workbook_version == 1


@pytest.mark.parametrize(
    ("candidate", "code"),
    [
        (mutation(expected_workbook_version=2), "STALE_SNAPSHOT"),
        (mutation(source_quantity=-1), "INSUFFICIENT_INVENTORY"),
    ],
)
def test_failed_precondition_or_negative_balance_leaves_original_unchanged(
    tmp_path: Path, candidate: WorkbookMutation, code: str
) -> None:
    path = tmp_path / "warehouse.xlsx"
    write_workbook(path)
    before = path.read_bytes()

    with pytest.raises(WorkbookFailure) as failure:
        WorkbookWriter(WorkbookRepository(tmp_path), path).commit(candidate)

    assert failure.value.code == code
    assert path.read_bytes() == before


def test_atomic_replace_failure_keeps_original_workbook(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.xlsx"
    write_workbook(path)
    before = path.read_bytes()

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise PermissionError("locked")

    writer = WorkbookWriter(WorkbookRepository(tmp_path), path, replace_file=fail_replace)

    with pytest.raises(WorkbookFailure) as failure:
        writer.commit(mutation())

    assert failure.value.code == "ATOMIC_REPLACE_FAILED"
    assert path.read_bytes() == before
    assert WorkbookRepository(tmp_path).load_snapshot(path).workbook_version == 0


def test_writer_worker_serializes_and_flushes_accepted_commands_on_stop(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.xlsx"
    write_workbook(path)
    worker = WorkbookWriterWorker(WorkbookWriter(WorkbookRepository(tmp_path), path), capacity=2)
    worker.start()

    first = worker.submit(mutation())
    duplicate = worker.submit(mutation(mutation_id=101))
    worker.stop(timeout_seconds=2)

    assert first.result().workbook_version_after == 1
    assert duplicate.result().workbook_version_after == 1
    assert not any(thread.name == worker.thread_name for thread in threading.enumerate())
    with pytest.raises(RuntimeError, match="stopped"):
        worker.submit(mutation(mutation_id=102))


def test_commit_receipt_result_is_stable_json(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.xlsx"
    write_workbook(path)

    receipt = WorkbookWriter(WorkbookRepository(tmp_path), path).commit(mutation())
    stored = (
        WorkbookRepository(tmp_path)
        .load_snapshot(path)
        .indexes.receipt_by_idempotency_key["event-100"]
    )

    assert json.loads(stored["result_json"]) == receipt.to_dict()

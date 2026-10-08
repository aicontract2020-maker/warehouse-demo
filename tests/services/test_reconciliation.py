from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.domain.event_machine import EventContext, EventSnapshot, EventState
from app.domain.mutations import (
    InventoryBalance,
    ObservedResult,
    PickTask,
    ReviewCommand,
    ReviewCommandType,
    TaskStatus,
)
from app.services.inventory_service import InventoryService
from app.services.reconciliation import Decision, ReconciliationService

NOW = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)


def task(**changes: object) -> PickTask:
    values: dict[str, object] = {
        "task_id": "task-1",
        "sku_id": "sku-box",
        "expected_quantity": 2,
        "unit": "case",
        "source_location_id": "A-03-01",
        "destination_location_id": "STAGING",
        "status": TaskStatus.IN_PROGRESS,
    }
    values.update(changes)
    return PickTask(**values)


def inventory(location: str, quantity: int, version: int = 3) -> InventoryBalance:
    return InventoryBalance(
        inventory_id=f"inventory-{location}",
        location_id=location,
        sku_id="sku-box",
        quantity=quantity,
        unit="case",
        version=version,
    )


def event(
    *,
    quantity: int = 2,
    confidence: float = 0.999,
    flags: frozenset[str] = frozenset(),
    state: EventState = EventState.COMPLETED,
    event_id: UUID | None = None,
) -> EventSnapshot:
    event_id = event_id or uuid4()
    context = EventContext(
        task_id="task-1",
        zone_profile_id="zone-1",
        zone_version=1,
        sku_id="sku-box",
        unit="case",
        source_location_id="A-03-01",
        destination_location_id="STAGING",
        source_session_id=uuid4(),
        configuration_version="cfg-1",
    )
    return EventSnapshot(
        event_id=event_id,
        state=state,
        started_source_timestamp_ms=1_000,
        ended_source_timestamp_ms=5_000,
        pick_count=max(quantity, 0),
        return_count=0,
        net_quantity=quantity,
        actions=(),
        aggregate_confidence=confidence,
        integrity_flags=flags,
        review_reason=None,
        context=context,
    )


def service() -> ReconciliationService:
    return ReconciliationService(
        InventoryService(), auto_approval_threshold=0.995, now=lambda: NOW
    )


def test_exact_match_auto_approves_and_builds_one_atomic_mutation() -> None:
    observed_event = event()

    result = service().reconcile(
        observed_event,
        task(),
        inventory("A-03-01", 10),
        inventory("STAGING", 4),
        evidence_path="evidence/event.mp4",
        persistence_writable=True,
    )

    assert result.decision is Decision.AUTO_APPROVED
    assert result.review_reason is None
    assert result.mismatched_fields == ()
    assert result.mutation is not None
    assert result.mutation.idempotency_key == str(observed_event.event_id)
    assert result.mutation.upserts["Inventory"][0]["quantity"] == 8
    assert result.mutation.upserts["Inventory"][1]["quantity"] == 6
    assert result.mutation.upserts["Tasks"][0]["status"] == "completed"
    assert result.mutation.upserts["Events"][0]["decision"] == "auto_approved"


@pytest.mark.parametrize(
    ("changed_task", "expected_field"),
    [
        ({"sku_id": "other"}, "sku_id"),
        ({"expected_quantity": 3}, "quantity"),
        ({"unit": "bag"}, "unit"),
        ({"source_location_id": "B-01"}, "source_location_id"),
    ],
)
def test_task_mismatch_requires_review_without_mutation(
    changed_task: dict[str, object], expected_field: str
) -> None:
    result = service().reconcile(
        event(),
        task(**changed_task),
        inventory("A-03-01", 10),
        inventory("STAGING", 4),
        evidence_path="evidence/event.mp4",
        persistence_writable=True,
    )

    assert result.decision is Decision.REVIEW_REQUIRED
    assert result.review_reason == "TASK_MISMATCH"
    assert expected_field in result.mismatched_fields
    assert result.mutation is None


@pytest.mark.parametrize(
    ("event_changes", "evidence_path", "persistence_writable", "reason"),
    [
        ({"confidence": 0.994}, "evidence/event.mp4", True, "LOW_CONFIDENCE"),
        (
            {"flags": frozenset({"TRACK_LOST_NEAR_BOUNDARY"})},
            "evidence/event.mp4",
            True,
            "TRACK_LOST_NEAR_BOUNDARY",
        ),
        ({}, None, True, "EVIDENCE_UNAVAILABLE"),
        ({}, "evidence/event.mp4", False, "PERSISTENCE_BLOCKED"),
    ],
)
def test_failed_auto_approval_gate_requires_review(
    event_changes: dict[str, object],
    evidence_path: str | None,
    persistence_writable: bool,
    reason: str,
) -> None:
    result = service().reconcile(
        event(**event_changes),
        task(),
        inventory("A-03-01", 10),
        inventory("STAGING", 4),
        evidence_path=evidence_path,
        persistence_writable=persistence_writable,
    )

    assert result.decision is Decision.REVIEW_REQUIRED
    assert result.review_reason == reason
    assert result.mutation is None


def test_insufficient_inventory_preserves_balance_and_requires_review() -> None:
    result = service().reconcile(
        event(quantity=2),
        task(),
        inventory("A-03-01", 1),
        inventory("STAGING", 4),
        evidence_path="evidence/event.mp4",
        persistence_writable=True,
    )

    assert result.decision is Decision.REVIEW_REQUIRED
    assert result.review_reason == "INSUFFICIENT_INVENTORY"
    assert result.mutation is None


def test_zero_net_is_a_persisted_no_op_and_negative_net_requires_review() -> None:
    zero = service().reconcile(
        event(quantity=0),
        task(expected_quantity=1),
        inventory("A-03-01", 10),
        inventory("STAGING", 4),
        evidence_path="evidence/event.mp4",
        persistence_writable=True,
    )
    negative = service().reconcile(
        event(quantity=-1, state=EventState.REVIEW_REQUIRED),
        task(),
        inventory("A-03-01", 10),
        inventory("STAGING", 4),
        evidence_path="evidence/event.mp4",
        persistence_writable=True,
    )

    assert zero.decision is Decision.NO_OP
    assert zero.mutation is not None
    assert zero.mutation.upserts.get("Inventory", ()) == ()
    assert negative.decision is Decision.REVIEW_REQUIRED
    assert negative.review_reason == "NEGATIVE_NET_QUANTITY"
    assert negative.mutation is None


def test_manual_edited_approval_requires_reason_and_preserves_original() -> None:
    observed_event = event(quantity=3, state=EventState.REVIEW_REQUIRED)
    command = ReviewCommand(
        command=ReviewCommandType.APPROVE_EDITED,
        operator_id="demo_operator",
        reason="Physical count confirmed two cases",
        final=ObservedResult(
            sku_id="sku-box",
            quantity=2,
            unit="case",
            source_location_id="A-03-01",
        ),
    )

    result = service().review(
        observed_event,
        task(),
        inventory("A-03-01", 10),
        inventory("STAGING", 4),
        command,
        idempotency_key="review-key-1",
    )

    assert result.decision is Decision.MANUAL_APPROVED
    assert result.mutation is not None
    review_row = result.mutation.appends["Reviews"][0]
    assert review_row["original_values"]["quantity"] == 3
    assert review_row["final_values"]["quantity"] == 2
    assert result.mutation.upserts["Inventory"][0]["quantity"] == 8


def test_manual_edit_and_reject_require_non_whitespace_reason() -> None:
    observed_event = event(state=EventState.REVIEW_REQUIRED)
    edited = ReviewCommand(
        command=ReviewCommandType.APPROVE_EDITED,
        operator_id="demo_operator",
        reason="  ",
        final=ObservedResult("sku-box", 2, "case", "A-03-01"),
    )
    rejected = ReviewCommand(
        command=ReviewCommandType.REJECT,
        operator_id="demo_operator",
        reason="",
    )

    with pytest.raises(ValueError, match="reason"):
        service().review(
            observed_event,
            task(),
            inventory("A-03-01", 10),
            inventory("STAGING", 4),
            edited,
            "review-key-2",
        )
    with pytest.raises(ValueError, match="reason"):
        service().review(
            observed_event,
            task(),
            inventory("A-03-01", 10),
            inventory("STAGING", 4),
            rejected,
            "review-key-3",
        )


def test_reject_audits_without_inventory_or_task_mutation() -> None:
    result = service().review(
        event(state=EventState.REVIEW_REQUIRED),
        task(),
        inventory("A-03-01", 10),
        inventory("STAGING", 4),
        ReviewCommand(
            command=ReviewCommandType.REJECT,
            operator_id="demo_operator",
            reason="Merged track",
        ),
        idempotency_key="review-key-4",
    )

    assert result.decision is Decision.REJECTED
    assert result.mutation is not None
    assert result.mutation.upserts.get("Inventory", ()) == ()
    assert result.mutation.upserts.get("Tasks", ()) == ()
    assert result.mutation.appends["Reviews"][0]["decision"] == "reject"


def test_repeated_event_builds_same_idempotency_key_and_effects_hash() -> None:
    observed_event = event(event_id=UUID(int=42))
    arguments = (
        observed_event,
        task(),
        inventory("A-03-01", 10),
        inventory("STAGING", 4),
    )

    first = service().reconcile(
        *arguments, evidence_path="evidence/event.mp4", persistence_writable=True
    )
    second = service().reconcile(
        *arguments, evidence_path="evidence/event.mp4", persistence_writable=True
    )

    assert first.mutation is not None and second.mutation is not None
    assert first.mutation.idempotency_key == second.mutation.idempotency_key
    assert first.mutation.canonical_effects_hash == second.mutation.canonical_effects_hash


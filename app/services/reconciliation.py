from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.domain.event_machine import EventSnapshot, EventState
from app.domain.mutations import (
    InventoryBalance,
    ObservedResult,
    PickTask,
    ReviewCommand,
    ReviewCommandType,
    TaskStatus,
    WorkbookMutation,
)
from app.services.inventory_service import InventoryMutationError, InventoryService

MANDATORY_INTEGRITY_FLAGS = frozenset(
    {
        "SOURCE_INTERRUPTED",
        "TRACK_LOST_NEAR_BOUNDARY",
        "OVERLAPPING_INSEPARABLE",
        "LATE_RESULT_DISCARDED",
        "EVIDENCE_UNAVAILABLE",
        "PERSISTENCE_BLOCKED",
    }
)


class Decision(StrEnum):
    AUTO_APPROVED = "auto_approved"
    MANUAL_APPROVED = "manual_approved"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"
    NO_OP = "no_op"


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    decision: Decision
    review_reason: str | None
    mismatched_fields: tuple[str, ...]
    mutation: WorkbookMutation | None


class ReconciliationService:
    def __init__(
        self,
        inventory_service: InventoryService,
        auto_approval_threshold: float = 0.995,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._inventory_service = inventory_service
        self._threshold = auto_approval_threshold
        self._now = now

    def reconcile(
        self,
        event: EventSnapshot,
        task: PickTask,
        source_inventory: InventoryBalance,
        destination_inventory: InventoryBalance,
        *,
        evidence_path: str | None,
        persistence_writable: bool,
        workbook_version: int = 0,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> ReconciliationResult:
        timing = {
            "workbook_version": workbook_version,
            "started_at": started_at,
            "ended_at": ended_at,
        }
        if event.event_id is None or event.context is None:
            raise ValueError("completed event identity and context are required")
        if event.net_quantity < 0:
            return self._review("NEGATIVE_NET_QUANTITY")
        if event.net_quantity == 0:
            mutation = self._inventory_service.build_non_inventory_mutation(
                event,
                task,
                Decision.NO_OP.value,
                str(event.event_id),
                self._now(),
                evidence_path,
                **timing,
            )
            return ReconciliationResult(Decision.NO_OP, None, (), mutation)
        if event.state is not EventState.COMPLETED:
            return self._review(event.review_reason or "EVENT_NOT_COMPLETED")

        observed = self._inventory_service.observed_result(event)
        mismatches = self._mismatches(observed, task)
        if mismatches:
            return ReconciliationResult(
                Decision.REVIEW_REQUIRED, "TASK_MISMATCH", mismatches, None
            )
        if task.status not in {TaskStatus.OPEN, TaskStatus.IN_PROGRESS}:
            return self._review("TASK_STATE_CHANGED")
        if event.aggregate_confidence is None or event.aggregate_confidence < self._threshold:
            return self._review("LOW_CONFIDENCE")
        blocking_flags = sorted(event.integrity_flags & MANDATORY_INTEGRITY_FLAGS)
        if blocking_flags:
            return self._review(blocking_flags[0])
        if evidence_path is None:
            return self._review("EVIDENCE_UNAVAILABLE")
        if not persistence_writable:
            return self._review("PERSISTENCE_BLOCKED")

        try:
            mutation = self._inventory_service.build_approval_mutation(
                event,
                task,
                source_inventory,
                destination_inventory,
                observed,
                Decision.AUTO_APPROVED.value,
                str(event.event_id),
                self._now(),
                evidence_path,
                **timing,
            )
        except InventoryMutationError as error:
            return self._review(error.code)
        return ReconciliationResult(Decision.AUTO_APPROVED, None, (), mutation)

    def review(
        self,
        event: EventSnapshot,
        task: PickTask,
        source_inventory: InventoryBalance,
        destination_inventory: InventoryBalance,
        command: ReviewCommand,
        idempotency_key: str,
        *,
        workbook_version: int = 0,
    ) -> ReconciliationResult:
        if event.state is not EventState.REVIEW_REQUIRED:
            raise ValueError("EVENT_NOT_REVIEWABLE")
        if not command.operator_id.strip():
            raise ValueError("operator_id is required")
        if command.command in {
            ReviewCommandType.APPROVE_EDITED,
            ReviewCommandType.REJECT,
        } and (not command.reason or not command.reason.strip()):
            raise ValueError("review reason is required")

        if command.command is ReviewCommandType.REJECT:
            mutation = self._inventory_service.build_non_inventory_mutation(
                event,
                task,
                Decision.REJECTED.value,
                idempotency_key,
                self._now(),
                None,
                command,
                workbook_version=workbook_version,
            )
            return ReconciliationResult(Decision.REJECTED, None, (), mutation)

        observed = self._inventory_service.observed_result(event)
        final = observed if command.command is ReviewCommandType.APPROVE_OBSERVED else command.final
        if final is None:
            raise ValueError("final values are required")
        review_command = ReviewCommand(
            command=command.command,
            operator_id=command.operator_id,
            reason=command.reason,
            final=final,
        )
        try:
            mutation = self._inventory_service.build_approval_mutation(
                event,
                task,
                source_inventory,
                destination_inventory,
                final,
                Decision.MANUAL_APPROVED.value,
                idempotency_key,
                self._now(),
                None,
                review_command,
                workbook_version=workbook_version,
            )
        except InventoryMutationError as error:
            return self._review(error.code)
        return ReconciliationResult(Decision.MANUAL_APPROVED, None, (), mutation)

    @staticmethod
    def _mismatches(observed: ObservedResult, task: PickTask) -> tuple[str, ...]:
        differences: list[str] = []
        if observed.sku_id != task.sku_id:
            differences.append("sku_id")
        if observed.quantity != task.expected_quantity:
            differences.append("quantity")
        if observed.unit != task.unit:
            differences.append("unit")
        if observed.source_location_id != task.source_location_id:
            differences.append("source_location_id")
        return tuple(differences)

    @staticmethod
    def _review(reason: str) -> ReconciliationResult:
        return ReconciliationResult(Decision.REVIEW_REQUIRED, reason, (), None)

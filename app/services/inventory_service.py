from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.domain.event_machine import EventSnapshot
from app.domain.mutations import (
    InventoryBalance,
    MutationPrecondition,
    ObservedResult,
    PickTask,
    ReviewCommand,
    WorkbookMutation,
    canonical_hash,
    stable_uuid,
)


class InventoryMutationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InventoryService:
    def build_approval_mutation(
        self,
        event: EventSnapshot,
        task: PickTask,
        source: InventoryBalance,
        destination: InventoryBalance,
        final: ObservedResult,
        decision: str,
        idempotency_key: str,
        decided_at: datetime,
        evidence_path: str | None,
        review: ReviewCommand | None = None,
    ) -> WorkbookMutation:
        self._validate_inventory(source, destination, task, final)
        if source.quantity < final.quantity:
            raise InventoryMutationError("INSUFFICIENT_INVENTORY")

        mutation_id = stable_uuid(idempotency_key, "mutation")
        timestamp = decided_at.isoformat()
        source_row = {
            "inventory_id": source.inventory_id,
            "location_id": source.location_id,
            "sku_id": final.sku_id,
            "quantity": source.quantity - final.quantity,
            "unit": final.unit,
            "version": source.version + 1,
            "updated_at": timestamp,
            "last_event_id": str(event.event_id),
        }
        destination_row = {
            "inventory_id": destination.inventory_id,
            "location_id": destination.location_id,
            "sku_id": final.sku_id,
            "quantity": destination.quantity + final.quantity,
            "unit": final.unit,
            "version": destination.version + 1,
            "updated_at": timestamp,
            "last_event_id": str(event.event_id),
        }
        event_row = self._event_row(
            event, final, decision, mutation_id, timestamp, evidence_path
        )
        task_row = {
            "task_id": task.task_id,
            "status": "completed",
            "updated_at": timestamp,
            "completed_event_id": str(event.event_id),
        }
        appends = self._common_appends(
            event, idempotency_key, mutation_id, decision, timestamp, review
        )
        upserts = {
            "Events": (event_row,),
            "Inventory": (source_row, destination_row),
            "Tasks": (task_row,),
        }
        return self._mutation(
            event,
            idempotency_key,
            mutation_id,
            source.version,
            task,
            upserts,
            appends,
            {"decision": decision, "final": final, "task_id": task.task_id},
        )

    def build_non_inventory_mutation(
        self,
        event: EventSnapshot,
        task: PickTask,
        decision: str,
        idempotency_key: str,
        decided_at: datetime,
        evidence_path: str | None,
        review: ReviewCommand | None = None,
    ) -> WorkbookMutation:
        mutation_id = stable_uuid(idempotency_key, "mutation")
        timestamp = decided_at.isoformat()
        final = self.observed_result(event) if decision == "no_op" else None
        upserts = {
            "Events": (
                self._event_row(
                    event, final, decision, mutation_id, timestamp, evidence_path
                ),
            )
        }
        appends = self._common_appends(
            event, idempotency_key, mutation_id, decision, timestamp, review
        )
        return self._mutation(
            event,
            idempotency_key,
            mutation_id,
            0,
            task,
            upserts,
            appends,
            {"decision": decision, "task_id": task.task_id},
        )

    @staticmethod
    def observed_result(event: EventSnapshot) -> ObservedResult:
        if event.context is None:
            raise ValueError("event context is required")
        return ObservedResult(
            sku_id=event.context.sku_id,
            quantity=event.net_quantity,
            unit=event.context.unit,
            source_location_id=event.context.source_location_id,
        )

    @staticmethod
    def _validate_inventory(
        source: InventoryBalance,
        destination: InventoryBalance,
        task: PickTask,
        final: ObservedResult,
    ) -> None:
        if (
            source.location_id != final.source_location_id
            or source.sku_id != final.sku_id
            or source.unit != final.unit
            or destination.location_id != task.destination_location_id
            or destination.sku_id != final.sku_id
            or destination.unit != final.unit
        ):
            raise InventoryMutationError("FINAL_VALUES_INVALID")

    @staticmethod
    def _event_row(
        event: EventSnapshot,
        final: ObservedResult | None,
        decision: str,
        mutation_id: UUID,
        timestamp: str,
        evidence_path: str | None,
    ) -> dict[str, object]:
        observed = InventoryService.observed_result(event)
        return {
            "event_id": str(event.event_id),
            "state": "rejected" if decision == "rejected" else "approved",
            "observed_sku_id": observed.sku_id,
            "observed_quantity": observed.quantity,
            "observed_unit": observed.unit,
            "observed_source_location_id": observed.source_location_id,
            "final_sku_id": final.sku_id if final else None,
            "final_quantity": final.quantity if final else None,
            "final_unit": final.unit if final else None,
            "final_source_location_id": final.source_location_id if final else None,
            "aggregate_confidence": event.aggregate_confidence,
            "integrity_flags": sorted(event.integrity_flags),
            "review_reason": event.review_reason,
            "decision": decision,
            "evidence_path": evidence_path,
            "decided_at": timestamp,
            "applied_at": timestamp if final and final.quantity > 0 else None,
            "mutation_id": str(mutation_id),
        }

    @staticmethod
    def _common_appends(
        event: EventSnapshot,
        idempotency_key: str,
        mutation_id: UUID,
        decision: str,
        timestamp: str,
        review: ReviewCommand | None,
    ) -> dict[str, tuple[dict[str, object], ...]]:
        audit = {
            "audit_id": str(stable_uuid(idempotency_key, "audit")),
            "timestamp": timestamp,
            "operator_id": review.operator_id if review else "system",
            "action": decision,
            "entity_type": "event",
            "entity_id": str(event.event_id),
            "result": "success",
        }
        receipt = {
            "mutation_id": str(mutation_id),
            "idempotency_key": idempotency_key,
            "event_id": str(event.event_id),
            "committed_at": None,
        }
        appends: dict[str, tuple[dict[str, object], ...]] = {
            "AuditLog": (audit,),
            "MutationReceipts": (receipt,),
            "EventActions": tuple(
                {
                    "action_id": str(item.action_id),
                    "event_id": str(item.event_id),
                    "action_sequence": item.action_sequence,
                    "track_id": item.track_id,
                    "transition_index": item.transition_index,
                    "action_type": item.action_type.value,
                    "delta": item.delta,
                    "source_timestamp_ms": item.source_timestamp_ms,
                    "frame_sequence": item.frame_sequence,
                    "confidence": item.confidence,
                    "bbox": item.bbox_xyxy,
                }
                for item in event.actions
            ),
        }
        if review:
            original = InventoryService.observed_result(event)
            final = review.final
            appends["Reviews"] = (
                {
                    "review_id": str(stable_uuid(idempotency_key, "review")),
                    "event_id": str(event.event_id),
                    "decision": review.command.value,
                    "operator_id": review.operator_id,
                    "original_values": {
                        "sku_id": original.sku_id,
                        "quantity": original.quantity,
                        "unit": original.unit,
                        "source_location_id": original.source_location_id,
                    },
                    "final_values": None
                    if final is None
                    else {
                        "sku_id": final.sku_id,
                        "quantity": final.quantity,
                        "unit": final.unit,
                        "source_location_id": final.source_location_id,
                    },
                    "reason": review.reason,
                    "created_at": timestamp,
                },
            )
        return appends

    @staticmethod
    def _mutation(
        event: EventSnapshot,
        idempotency_key: str,
        mutation_id: UUID,
        source_version: int,
        task: PickTask,
        upserts: dict[str, tuple[dict[str, object], ...]],
        appends: dict[str, tuple[dict[str, object], ...]],
        canonical_effects: object,
    ) -> WorkbookMutation:
        return WorkbookMutation(
            mutation_id=mutation_id,
            idempotency_key=idempotency_key,
            request_id=None,
            expected_workbook_version=0,
            preconditions=(
                MutationPrecondition(
                    "event", str(event.event_id), {"mutation_id": None}
                ),
                MutationPrecondition(
                    "task", task.task_id, {"status": task.status.value}
                ),
                MutationPrecondition(
                    "inventory", task.source_location_id, {"version": source_version}
                ),
            ),
            upserts=upserts,
            appends=appends,
            deletes={},
            canonical_effects_hash=canonical_hash(canonical_effects),
        )

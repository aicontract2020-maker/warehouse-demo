from __future__ import annotations

import json
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


def _json(value: object) -> str:
    """Canonical compact JSON for workbook ``*_json`` cells."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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
        *,
        workbook_version: int = 0,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
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
            event, final, decision, mutation_id, timestamp, evidence_path,
            review=review, started_at=started_at, ended_at=ended_at,
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
        inventory_checks = tuple(
            MutationPrecondition(
                "inventory",
                balance.inventory_id,
                {"version": balance.version, "quantity": balance.quantity},
            )
            for balance in (source, destination)
        )
        return self._mutation(
            event,
            idempotency_key,
            mutation_id,
            workbook_version,
            task,
            inventory_checks,
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
        *,
        workbook_version: int = 0,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> WorkbookMutation:
        mutation_id = stable_uuid(idempotency_key, "mutation")
        timestamp = decided_at.isoformat()
        final = self.observed_result(event) if decision == "no_op" else None
        upserts = {
            "Events": (
                self._event_row(
                    event, final, decision, mutation_id, timestamp, evidence_path,
                    review=review, started_at=started_at, ended_at=ended_at,
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
            workbook_version,
            task,
            (),
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
        *,
        review: ReviewCommand | None,
        started_at: datetime | None,
        ended_at: datetime | None,
    ) -> dict[str, object]:
        observed = InventoryService.observed_result(event)
        context = event.context
        assert context is not None  # observed_result already enforced this
        state = {"rejected": "rejected", "no_op": "completed"}.get(decision, "approved")
        row: dict[str, object] = {
            "event_id": str(event.event_id),
            "session_id": str(context.source_session_id),
            "task_id": context.task_id,
            "zone_profile_id": context.zone_profile_id,
            "zone_version": context.zone_version,
            "state": state,
            "observed_sku_id": observed.sku_id,
            "observed_quantity": observed.quantity,
            "observed_unit": observed.unit,
            "observed_source_location_id": observed.source_location_id,
            "final_sku_id": final.sku_id if final else None,
            "final_quantity": final.quantity if final else None,
            "final_unit": final.unit if final else None,
            "final_source_location_id": final.source_location_id if final else None,
            "aggregate_confidence": event.aggregate_confidence,
            "integrity_flags_json": _json(sorted(event.integrity_flags)),
            "review_reason": event.review_reason,
            "decision": decision,
            "decided_at": timestamp,
            "applied_at": timestamp if final and final.quantity > 0 else None,
            "mutation_id": str(mutation_id),
        }
        # Upserts merge into the stored row, so leaving a key out keeps the value that
        # was written when the event was first persisted (e.g. at review time).
        optional = {
            "evidence_path": evidence_path,
            "started_at": started_at.isoformat() if started_at else None,
            "ended_at": ended_at.isoformat() if ended_at else None,
            "created_at": timestamp if review is None else None,
        }
        row.update({key: value for key, value in optional.items() if value is not None})
        return row

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
        }
        if review is None:
            # A reviewed event already stored its actions when it was marked for review.
            appends["EventActions"] = tuple(
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
                    "bbox_json": _json(list(item.bbox_xyxy)),
                    "created_at": timestamp,
                }
                for item in event.actions
            )
        if review:
            original = InventoryService.observed_result(event)
            final = review.final
            appends["Reviews"] = (
                {
                    "review_id": str(stable_uuid(idempotency_key, "review")),
                    "event_id": str(event.event_id),
                    "decision": review.command.value,
                    "operator_id": review.operator_id,
                    "original_values_json": _json({
                        "sku_id": original.sku_id,
                        "quantity": original.quantity,
                        "unit": original.unit,
                        "source_location_id": original.source_location_id,
                    }),
                    "final_values_json": None
                    if final is None
                    else _json({
                        "sku_id": final.sku_id,
                        "quantity": final.quantity,
                        "unit": final.unit,
                        "source_location_id": final.source_location_id,
                    }),
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
        workbook_version: int,
        task: PickTask,
        inventory_checks: tuple[MutationPrecondition, ...],
        upserts: dict[str, tuple[dict[str, object], ...]],
        appends: dict[str, tuple[dict[str, object], ...]],
        canonical_effects: object,
    ) -> WorkbookMutation:
        return WorkbookMutation(
            mutation_id=mutation_id,
            idempotency_key=idempotency_key,
            request_id=None,
            expected_workbook_version=workbook_version,
            preconditions=(
                MutationPrecondition(
                    "event", str(event.event_id), {"mutation_id": None}
                ),
                MutationPrecondition(
                    "task", task.task_id, {"status": task.status.value}
                ),
                *inventory_checks,
            ),
            upserts=upserts,
            appends=appends,
            deletes={},
            canonical_effects_hash=canonical_hash(canonical_effects),
        )

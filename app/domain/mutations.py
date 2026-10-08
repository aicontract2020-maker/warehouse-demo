from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ReviewCommandType(StrEnum):
    APPROVE_OBSERVED = "approve_observed"
    APPROVE_EDITED = "approve_edited"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ObservedResult:
    sku_id: str
    quantity: int
    unit: str
    source_location_id: str

    def __post_init__(self) -> None:
        if not self.sku_id or not self.unit or not self.source_location_id:
            raise ValueError("observed SKU, unit, and source location are required")
        if self.quantity < 0:
            raise ValueError("quantity must be non-negative")


@dataclass(frozen=True, slots=True)
class PickTask:
    task_id: str
    sku_id: str
    expected_quantity: int
    unit: str
    source_location_id: str
    destination_location_id: str
    status: TaskStatus

    def __post_init__(self) -> None:
        if self.expected_quantity <= 0:
            raise ValueError("task expected quantity must be positive")


@dataclass(frozen=True, slots=True)
class InventoryBalance:
    inventory_id: str
    location_id: str
    sku_id: str
    quantity: int
    unit: str
    version: int

    def __post_init__(self) -> None:
        if self.quantity < 0 or self.version < 1:
            raise ValueError("inventory quantity and version are invalid")


@dataclass(frozen=True, slots=True)
class ReviewCommand:
    command: ReviewCommandType
    operator_id: str
    reason: str | None = None
    final: ObservedResult | None = None


@dataclass(frozen=True, slots=True)
class MutationPrecondition:
    entity: str
    entity_id: str
    expected: dict[str, object]


@dataclass(frozen=True, slots=True)
class WorkbookMutation:
    mutation_id: UUID
    idempotency_key: str
    request_id: UUID | None
    expected_workbook_version: int
    preconditions: tuple[MutationPrecondition, ...]
    upserts: dict[str, tuple[dict[str, object], ...]]
    appends: dict[str, tuple[dict[str, object], ...]]
    deletes: dict[str, tuple[str, ...]]
    canonical_effects_hash: str


def stable_uuid(idempotency_key: str, purpose: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"pick-zone:{idempotency_key}:{purpose}")


def canonical_hash(value: object) -> str:
    encoded = json.dumps(
        _to_json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _to_json_value(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _to_json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_to_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_to_json_value(item) for item in value)
    if isinstance(value, (UUID, datetime, StrEnum)):
        return str(value)
    return value

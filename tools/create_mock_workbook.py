"""Write a small, coherent mock workbook for trying the demo without real master data.

Usage (from the repository root)::

    uv run --frozen python -m tools.create_mock_workbook            # data/pick-zone-demo.xlsx
    uv run --frozen python -m tools.create_mock_workbook PATH --force

The data follows docs/data-model.md. ZoneProfiles has no SKU/location columns, so the zone is
linked to its pick location by name (zone ``name`` == ``Locations.location_id``) and to its SKU
through the single inventory balance stocked at that location.

The file source plays ``data/test1.mp4`` (a local video that is not in the repository).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.context import DEFAULT_WORKBOOK_NAME
from app.persistence.workbook import WorkbookRepository
from app.persistence.writer import WorkbookWriter

Rows = dict[str, list[dict[str, object]]]

CREATED_AT = "2026-10-08T16:00:00Z"
SCENARIO_ID = "mock-pick-zone"
WORKBOOK_ID = "6d6f636b-0000-4000-8000-000000000001"
SOURCE_PROFILE_ID = "SRC-FILE-01"
ZONE_PROFILE_ID = "ZONE-A-03-01"
ZONE_LOCATION_ID = "A-03-01"

# sku_id, description, unit, detector_class, active
SKUS = (
    ("SKU-OIL-5L", "Cooking oil 5 L, case of 4", "case", "box", True),
    ("SKU-RICE-10KG", "Rice 10 kg bag", "bag", "bag", True),
    ("SKU-TEA-12", "Green tea, 12-pack", "piece", "carton", True),
    ("SKU-SOAP-OLD", "Discontinued soap bar", "piece", "soap", False),
)
# location_id, description, location_type
LOCATIONS = (
    ("A-03-01", "Aisle A bay 3 level 1", "pick_zone"),
    ("A-03-02", "Aisle A bay 3 level 2", "pick_zone"),
    ("B-01-01", "Aisle B bay 1 level 1", "pick_zone"),
    ("STAGING-01", "Outbound staging lane 1", "staging"),
    ("STAGING-02", "Outbound staging lane 2", "staging"),
)
# location_id, sku_id, quantity
INVENTORY = (
    ("A-03-01", "SKU-OIL-5L", 24),
    ("A-03-02", "SKU-RICE-10KG", 40),
    ("B-01-01", "SKU-TEA-12", 120),
    ("STAGING-01", "SKU-OIL-5L", 0),
    ("STAGING-01", "SKU-RICE-10KG", 0),
    ("STAGING-02", "SKU-TEA-12", 0),
)
# task_id, order_id, sku_id, expected_quantity, source, destination, selected
TASKS = (
    ("PICK-1001", "ORD-5001", "SKU-OIL-5L", 2, "A-03-01", "STAGING-01", True),
    ("PICK-1002", "ORD-5002", "SKU-RICE-10KG", 1, "A-03-02", "STAGING-01", False),
    ("PICK-1003", "ORD-5003", "SKU-TEA-12", 3, "B-01-01", "STAGING-02", False),
)


def _polygon(*points: tuple[float, float]) -> str:
    return json.dumps([list(point) for point in points], separators=(",", ":"))


def mock_rows() -> Rows:
    """Return the mock workbook rows keyed by sheet name."""
    units = {sku_id: unit for sku_id, _, unit, _, _ in SKUS}
    stamps = {"created_at": CREATED_AT, "updated_at": CREATED_AT}
    meta = {
        "schema_version": "1",
        "workbook_id": WORKBOOK_ID,
        "scenario_id": SCENARIO_ID,
        "created_at": CREATED_AT,
        "last_committed_at": "",
        "last_mutation_id": "",
    }
    return {
        "WorkbookMeta": [
            {"key": key, "value": value, "updated_at": CREATED_AT} for key, value in meta.items()
        ],
        "Skus": [
            {
                "sku_id": sku_id,
                "description": description,
                "unit": unit,
                "detector_class": detector_class,
                "reference_image_path": f"references/{sku_id.lower()}.jpg",
                "active": active,
                **stamps,
            }
            for sku_id, description, unit, detector_class, active in SKUS
        ],
        "Locations": [
            {
                "location_id": location_id,
                "description": description,
                "location_type": location_type,
                "active": True,
                **stamps,
            }
            for location_id, description, location_type in LOCATIONS
        ],
        "Inventory": [
            {
                "inventory_id": f"INV-{location_id}-{sku_id}",
                "location_id": location_id,
                "sku_id": sku_id,
                "quantity": quantity,
                "unit": units[sku_id],
                "version": 1,
                "updated_at": CREATED_AT,
                "last_event_id": "",
            }
            for location_id, sku_id, quantity in INVENTORY
        ],
        "Tasks": [
            {
                "task_id": task_id,
                "order_id": order_id,
                "sku_id": sku_id,
                "expected_quantity": quantity,
                "unit": units[sku_id],
                "source_location_id": source,
                "destination_location_id": destination,
                "status": "open",
                "selected": selected,
                **stamps,
                "completed_event_id": "",
            }
            for task_id, order_id, sku_id, quantity, source, destination, selected in TASKS
        ],
        "SourceProfiles": [
            {
                "source_profile_id": SOURCE_PROFILE_ID,
                "name": "Kai's pick-zone video (data/test1.mp4)",
                "source_type": "file",
                # Relative to the data dir: data/test1.mp4 (local only, *.mp4 is git-ignored).
                "file_path": "test1.mp4",
                "camera_index": "",
                "network_url_redacted": "",
                "analysis_fps": 4,
                "preview_fps": 10,
                "requested_width": 1280,
                "requested_height": 720,
                "reconnect_enabled": False,
                "active": True,
                **stamps,
            }
        ],
        "ZoneProfiles": [
            {
                "zone_profile_id": ZONE_PROFILE_ID,
                "source_profile_id": SOURCE_PROFILE_ID,
                "name": ZONE_LOCATION_ID,  # the zone's configured location
                "version": 1,
                "source_width": 1280,
                "source_height": 720,
                "shelf_polygon_json": _polygon((0.05, 0.1), (0.5, 0.1), (0.5, 0.85), (0.05, 0.85)),
                "interaction_polygon_json": _polygon(
                    (0.2, 0.05), (0.8, 0.05), (0.8, 0.95), (0.2, 0.95)
                ),
                "exit_polygon_json": _polygon((0.5, 0.05), (0.98, 0.05), (0.98, 0.95), (0.5, 0.95)),
                "counting_line_json": _polygon((0.5, 0.05), (0.5, 0.95)),
                "shelf_side_sign": 1,
                "uncertainty_band_norm": 0.02,
                "crossing_confirm_frames": 2,
                "quiet_seconds": 3,
                "stable_seconds": 2,
                "hard_idle_seconds": 10,
                "merge_window_seconds": 10,
                "active": True,
                **stamps,
            }
        ],
        "Sessions": [],
        "Events": [],
        "EventActions": [],
        "Reviews": [],
        "AuditLog": [
            {
                "audit_id": "6d6f636b-0000-4000-8000-0000000000a1",
                "timestamp": CREATED_AT,
                "operator_id": "system",
                "action": "fixture_created",
                "entity_type": "scenario",
                "entity_id": SCENARIO_ID,
                "request_id": "",
                "before_json": "",
                "after_json": "",
                "result": "success",
                "error_code": "",
                "details_json": "{}",
            }
        ],
        "MutationReceipts": [],
    }


def write_mock_workbook(path: Path, *, overwrite: bool = False) -> Path:
    """Write the mock workbook to ``path`` and check that it loads; returns the resolved path."""
    path = path.expanduser().resolve()
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists (use --force to replace it)")
    path.parent.mkdir(parents=True, exist_ok=True)
    WorkbookWriter._save_and_sync(path, mock_rows())
    WorkbookRepository(path.parent).load_snapshot(path)  # raises WorkbookFailure if invalid
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", type=Path, default=Path("data") / DEFAULT_WORKBOOK_NAME)
    parser.add_argument("--force", action="store_true", help="replace an existing workbook")
    args = parser.parse_args(argv)
    try:
        path = write_mock_workbook(args.path, overwrite=args.force)
    except FileExistsError as error:
        print(error)
        return 1
    print(f"wrote mock workbook {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from pathlib import Path

from app.core.context import _inside
from app.core.settings import Settings
from app.persistence.workbook import WorkbookRepository
from tools.create_mock_workbook import main, mock_rows, write_mock_workbook


def test_mock_workbook_loads_and_its_master_data_is_coherent(tmp_path: Path) -> None:
    path = write_mock_workbook(tmp_path / "pick-zone-demo.xlsx")

    snapshot = WorkbookRepository(tmp_path).load_snapshot(path)
    indexes = snapshot.indexes
    assert (snapshot.scenario_id, snapshot.workbook_version) == ("mock-pick-zone", 0)
    active_skus = [sku for sku in indexes.sku_by_id.values() if sku["active"]]
    assert len(active_skus) >= 3
    assert len({sku["detector_class"] for sku in active_skus}) == len(active_skus)
    assert len(indexes.task_by_id) >= 3
    for task in indexes.task_by_id.values():
        assert task["status"] == "open"
        sku = indexes.sku_by_id[task["sku_id"]]
        assert sku["active"] and sku["unit"] == task["unit"]
        for column, location_type in (
            ("source_location_id", "pick_zone"),
            ("destination_location_id", "staging"),
        ):
            location = indexes.location_by_id[task[column]]
            assert location["active"] and location["location_type"] == location_type
            balance = indexes.inventory_by_location_sku[(task[column], task["sku_id"])]
            assert balance["unit"] == task["unit"]
        source = indexes.inventory_by_location_sku[(task["source_location_id"], task["sku_id"])]
        assert source["quantity"] >= task["expected_quantity"]
    assert indexes.selected_task is not None
    assert indexes.selected_task["task_id"] == "PICK-1001"


def test_mock_zone_is_linked_to_one_location_and_through_it_one_sku(tmp_path: Path) -> None:
    path = write_mock_workbook(tmp_path / "pick-zone-demo.xlsx")

    indexes = WorkbookRepository(tmp_path).load_snapshot(path).indexes

    assert list(indexes.source_profile_by_id) == ["SRC-FILE-01"]
    assert indexes.source_profile_by_id["SRC-FILE-01"]["source_type"] == "file"
    zone = indexes.active_zone_by_source["SRC-FILE-01"]
    assert zone.zone_profile_id == "ZONE-A-03-01"
    assert indexes.location_by_id[zone.name]["location_type"] == "pick_zone"
    stocked_here = {
        sku for location, sku in indexes.inventory_by_location_sku if location == zone.name
    }
    selected = indexes.selected_task
    assert selected is not None
    assert stocked_here == {selected["sku_id"]}
    assert selected["source_location_id"] == zone.name


def test_mock_rows_are_deterministic() -> None:
    assert mock_rows() == mock_rows()


def test_cli_writes_the_workbook_and_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    target = tmp_path / "data" / "pick-zone-demo.xlsx"

    assert main([str(target)]) == 0
    assert target.is_file()
    assert main([str(target)]) == 1
    assert main([str(target), "--force"]) == 0


def test_mock_source_plays_kais_local_video_from_the_data_dir(tmp_path: Path) -> None:
    path = write_mock_workbook(tmp_path / "pick-zone-demo.xlsx")
    profile = WorkbookRepository(tmp_path).load_snapshot(path).indexes.source_profile_by_id[
        "SRC-FILE-01"
    ]
    settings = Settings(project_dir=tmp_path)
    assert settings.data_dir is not None

    # file_path is stored relative to the data dir, so data/test1.mp4 is "test1.mp4".
    assert profile["file_path"] == "test1.mp4"
    assert _inside(settings.data_dir, str(profile["file_path"])) == (
        tmp_path / "data" / "test1.mp4"
    ).resolve()

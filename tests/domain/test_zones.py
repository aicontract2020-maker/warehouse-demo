from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.models import BoundarySide, RegionMembership
from app.domain.zones import ZoneProfile


def valid_zone_data() -> dict[str, object]:
    return {
        "zone_profile_id": "zone-a-03-01",
        "source_profile_id": "source-file-1",
        "name": "A-03-01",
        "version": 1,
        "source_width": 1280,
        "source_height": 720,
        "shelf_polygon": ((0.05, 0.10), (0.50, 0.10), (0.50, 0.85), (0.05, 0.85)),
        "interaction_polygon": (
            (0.20, 0.05),
            (0.80, 0.05),
            (0.80, 0.95),
            (0.20, 0.95),
        ),
        "exit_polygon": ((0.50, 0.05), (0.98, 0.05), (0.98, 0.95), (0.50, 0.95)),
        "counting_line": ((0.50, 0.05), (0.50, 0.95)),
        "shelf_side_sign": 1,
        "uncertainty_band_norm": 0.02,
        "crossing_confirm_frames": 2,
        "quiet_seconds": 3.0,
        "stable_seconds": 2.0,
        "hard_idle_seconds": 10.0,
        "merge_window_seconds": 10.0,
        "active": True,
    }


def test_zone_profile_round_trips_as_immutable_normalized_geometry() -> None:
    profile = ZoneProfile.model_validate(valid_zone_data())

    restored = ZoneProfile.model_validate_json(profile.model_dump_json())

    assert restored == profile
    assert restored.model_config["frozen"] is True
    with pytest.raises(ValidationError):
        restored.source_width = 640  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "shelf_polygon",
            ((0.0, 0.0), (1.1, 0.0), (0.5, 0.5)),
            "normalized",
        ),
        (
            "interaction_polygon",
            ((0.0, 0.0), (1.0, 1.0), (0.0, 1.0), (1.0, 0.0)),
            "self-intersecting",
        ),
        (
            "exit_polygon",
            ((0.0, 0.0), (0.5, 0.5), (1.0, 1.0)),
            "non-collinear",
        ),
        ("counting_line", ((0.5, 0.5), (0.5, 0.5)), "distinct"),
    ],
)
def test_zone_profile_rejects_invalid_geometry(
    field: str, value: object, message: str
) -> None:
    data = valid_zone_data()
    data[field] = value

    with pytest.raises(ValidationError, match=message):
        ZoneProfile.model_validate(data)


def test_zone_profile_rejects_inconsistent_event_timers() -> None:
    data = valid_zone_data()
    data["hard_idle_seconds"] = 4.9

    with pytest.raises(ValidationError, match="quiet_seconds plus stable_seconds"):
        ZoneProfile.model_validate(data)


def test_zone_classifies_regions_and_directional_line_sides() -> None:
    profile = ZoneProfile.model_validate(valid_zone_data())

    shelf = profile.locate((0.25, 0.50))
    exit_region = profile.locate((0.90, 0.50))
    uncertain = profile.locate((0.505, 0.50))

    assert shelf == RegionMembership(
        in_shelf=True,
        in_interaction=True,
        in_exit=False,
        boundary_side=BoundarySide.SHELF,
    )
    assert exit_region == RegionMembership(
        in_shelf=False,
        in_interaction=False,
        in_exit=True,
        boundary_side=BoundarySide.EXIT,
    )
    assert uncertain.boundary_side is BoundarySide.UNCERTAIN


def test_zone_converts_normalized_points_to_source_pixels() -> None:
    profile = ZoneProfile.model_validate(valid_zone_data())

    assert profile.to_pixel_point((0.5, 0.25)) == (640, 180)
    assert profile.to_pixel_polygon(profile.shelf_polygon)[0] == (64, 72)


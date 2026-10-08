from __future__ import annotations

import math
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.models import BoundarySide, RegionMembership

NormalizedPoint = tuple[float, float]
NormalizedPolygon = tuple[NormalizedPoint, ...]

_GEOMETRY_EPSILON = 1e-9


def _cross(origin: NormalizedPoint, first: NormalizedPoint, second: NormalizedPoint) -> float:
    return (first[0] - origin[0]) * (second[1] - origin[1]) - (
        first[1] - origin[1]
    ) * (second[0] - origin[0])


def _point_on_segment(
    point: NormalizedPoint, start: NormalizedPoint, end: NormalizedPoint
) -> bool:
    if abs(_cross(start, end, point)) > _GEOMETRY_EPSILON:
        return False
    return (
        min(start[0], end[0]) - _GEOMETRY_EPSILON
        <= point[0]
        <= max(start[0], end[0]) + _GEOMETRY_EPSILON
        and min(start[1], end[1]) - _GEOMETRY_EPSILON
        <= point[1]
        <= max(start[1], end[1]) + _GEOMETRY_EPSILON
    )


def _segments_intersect(
    first_start: NormalizedPoint,
    first_end: NormalizedPoint,
    second_start: NormalizedPoint,
    second_end: NormalizedPoint,
) -> bool:
    orientations = (
        _cross(first_start, first_end, second_start),
        _cross(first_start, first_end, second_end),
        _cross(second_start, second_end, first_start),
        _cross(second_start, second_end, first_end),
    )
    if orientations[0] * orientations[1] < 0 and orientations[2] * orientations[3] < 0:
        return True
    return any(
        abs(value) <= _GEOMETRY_EPSILON and _point_on_segment(point, start, end)
        for value, point, start, end in (
            (orientations[0], second_start, first_start, first_end),
            (orientations[1], second_end, first_start, first_end),
            (orientations[2], first_start, second_start, second_end),
            (orientations[3], first_end, second_start, second_end),
        )
    )


def _validate_normalized_point(point: NormalizedPoint) -> None:
    if len(point) != 2 or not all(math.isfinite(value) and 0 <= value <= 1 for value in point):
        raise ValueError("geometry coordinates must be finite normalized values in [0, 1]")


def _validate_polygon(polygon: NormalizedPolygon) -> NormalizedPolygon:
    if len(polygon) < 3:
        raise ValueError("polygon requires at least three points")
    for point in polygon:
        _validate_normalized_point(point)
    if len(set(polygon)) != len(polygon):
        raise ValueError("polygon points must be distinct")

    edge_count = len(polygon)
    for first_index in range(edge_count):
        first_edge = (polygon[first_index], polygon[(first_index + 1) % edge_count])
        for second_index in range(first_index + 1, edge_count):
            if second_index in {
                first_index,
                (first_index + 1) % edge_count,
                (first_index - 1) % edge_count,
            }:
                continue
            second_edge = (polygon[second_index], polygon[(second_index + 1) % edge_count])
            if _segments_intersect(*first_edge, *second_edge):
                raise ValueError("polygon cannot be self-intersecting")

    signed_double_area = sum(
        start[0] * end[1] - end[0] * start[1]
        for start, end in zip(polygon, polygon[1:] + polygon[:1], strict=True)
    )
    if abs(signed_double_area) <= _GEOMETRY_EPSILON:
        raise ValueError("polygon points must be non-collinear")
    return polygon


def point_in_polygon(point: NormalizedPoint, polygon: NormalizedPolygon) -> bool:
    _validate_normalized_point(point)
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if _point_on_segment(point, previous, current):
            return True
        crosses_ray = (current[1] > point[1]) != (previous[1] > point[1])
        if crosses_ray:
            intersection_x = (previous[0] - current[0]) * (
                point[1] - current[1]
            ) / (previous[1] - current[1]) + current[0]
            if point[0] < intersection_x:
                inside = not inside
        previous = current
    return inside


class ZoneProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    zone_profile_id: str = Field(min_length=1)
    source_profile_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    source_width: int = Field(gt=0)
    source_height: int = Field(gt=0)
    shelf_polygon: NormalizedPolygon
    interaction_polygon: NormalizedPolygon
    exit_polygon: NormalizedPolygon
    counting_line: tuple[NormalizedPoint, NormalizedPoint]
    shelf_side_sign: int
    uncertainty_band_norm: float = Field(ge=0, le=0.2)
    crossing_confirm_frames: int = Field(ge=2)
    quiet_seconds: float = Field(gt=0)
    stable_seconds: float = Field(gt=0)
    hard_idle_seconds: float = Field(gt=0)
    merge_window_seconds: float = Field(ge=0)
    active: bool

    @field_validator("shelf_polygon", "interaction_polygon", "exit_polygon")
    @classmethod
    def validate_polygon(cls, polygon: NormalizedPolygon) -> NormalizedPolygon:
        return _validate_polygon(polygon)

    @field_validator("counting_line")
    @classmethod
    def validate_counting_line(
        cls, line: tuple[NormalizedPoint, NormalizedPoint]
    ) -> tuple[NormalizedPoint, NormalizedPoint]:
        for point in line:
            _validate_normalized_point(point)
        if line[0] == line[1]:
            raise ValueError("counting line endpoints must be distinct")
        return line

    @field_validator("shelf_side_sign")
    @classmethod
    def validate_shelf_side_sign(cls, sign: int) -> int:
        if sign not in {-1, 1}:
            raise ValueError("shelf_side_sign must be -1 or 1")
        return sign

    @model_validator(mode="after")
    def validate_timers(self) -> Self:
        if self.hard_idle_seconds < self.quiet_seconds + self.stable_seconds:
            raise ValueError("hard_idle_seconds must be at least quiet_seconds plus stable_seconds")
        return self

    def signed_line_distance(self, point: NormalizedPoint) -> float:
        _validate_normalized_point(point)
        start, end = self.counting_line
        delta_x = end[0] - start[0]
        delta_y = end[1] - start[1]
        return _cross(start, end, point) / math.hypot(delta_x, delta_y)

    def boundary_side(self, point: NormalizedPoint) -> BoundarySide:
        distance = self.signed_line_distance(point)
        if abs(distance) <= self.uncertainty_band_norm:
            return BoundarySide.UNCERTAIN
        if math.copysign(1, distance) == self.shelf_side_sign:
            return BoundarySide.SHELF
        return BoundarySide.EXIT

    def locate(self, point: NormalizedPoint) -> RegionMembership:
        return RegionMembership(
            in_shelf=point_in_polygon(point, self.shelf_polygon),
            in_interaction=point_in_polygon(point, self.interaction_polygon),
            in_exit=point_in_polygon(point, self.exit_polygon),
            boundary_side=self.boundary_side(point),
        )

    def to_pixel_point(self, point: NormalizedPoint) -> tuple[int, int]:
        _validate_normalized_point(point)
        return round(point[0] * self.source_width), round(point[1] * self.source_height)

    def to_pixel_polygon(self, polygon: NormalizedPolygon) -> tuple[tuple[int, int], ...]:
        return tuple(self.to_pixel_point(point) for point in polygon)

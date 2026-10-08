from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class BoundarySide(StrEnum):
    SHELF = "shelf"
    EXIT = "exit"
    UNCERTAIN = "uncertain"


class RegionMembership(BaseModel):
    model_config = ConfigDict(frozen=True)

    in_shelf: bool
    in_interaction: bool
    in_exit: bool
    boundary_side: BoundarySide


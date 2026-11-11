"""Shared string-valued domain Enums persisted verbatim in SQLite."""
from __future__ import annotations

from enum import Enum


class AssetStatus(str, Enum):
    ACTIVE = "active"
    DELETED = "deleted"

"""Base types and registry for Gadgetbridge type adapters.

A *RawObservation* is the intermediate format produced by adapters before
normalization.  It carries the source timestamp in Unix seconds and the
raw value dict extracted from the Gadgetbridge DB.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class RawObservation:
    """Intermediate observation produced by a type adapter."""

    source_timestamp_sec: int
    type: str
    value: dict[str, Any]
    source_table: str
    source_identity: str
    raw_fields: dict[str, Any] = field(default_factory=dict)


class TypeAdapter(Protocol):
    """Protocol that all type adapters implement."""

    @staticmethod
    def extract(
        conn: sqlite3.Connection,
        *,
        source_device_id: int,
        source_user_id: int,
    ) -> list[RawObservation]:
        ...


# Registry of known adapters, keyed by observation type string.
_ADAPTERS: dict[str, TypeAdapter] = {}


def register_adapter(obs_type: str) -> Callable[[type], type]:
    """Class decorator to register a type adapter."""

    def decorator(cls: type) -> type:
        _ADAPTERS[obs_type] = cls()  # type: ignore[assignment]
        return cls

    return decorator


def get_all_adapters() -> dict[str, TypeAdapter]:
    """Return a copy of the adapter registry."""
    return dict(_ADAPTERS)

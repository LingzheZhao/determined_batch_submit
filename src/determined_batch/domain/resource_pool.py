"""Domain objects for Determined resource pools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Slot:
    """Representation of a GPU slot in a resource pool."""

    slot_id: str
    agent_id: str
    agent_label: Optional[str] = None
    device: Optional[str] = None
    enabled: bool = True
    container_id: Optional[str] = None
    resource_pool: Optional[str] = None

    @classmethod
    def from_api_data(cls, data: Dict[str, Any]) -> "Slot":
        if not isinstance(data, dict):
            return cls(slot_id="", agent_id="")
        return cls(
            slot_id=str(data.get("slot_id", "")),
            agent_id=str(data.get("agent_id", "")),
            agent_label=data.get("agent_label"),
            device=data.get("device"),
            enabled=bool(data.get("enabled", True)),
            container_id=data.get("container_id"),
            resource_pool=data.get("resource_pool"),
        )

    def is_available(self) -> bool:
        return self.enabled and self.container_id is None


@dataclass
class ResourcePool:
    """Representation of a Determined resource pool."""

    name: str
    description: Optional[str] = None
    slots: Optional[List[Slot]] = None
    total_slots: int = 0
    used_slots: int = 0
    available_slots: int = 0

    @classmethod
    def from_api_data(cls, data: Dict[str, Any], slots: Optional[List[Slot]] = None) -> "ResourcePool":
        pool_name = data.get("name", "") if isinstance(data, dict) else ""
        total_slots = 0
        used_slots = 0
        available_slots = 0
        if slots:
            pool_slots = [s for s in slots if s.resource_pool == pool_name]
            total_slots = len(pool_slots)
            used_slots = len([s for s in pool_slots if not s.is_available()])
            available_slots = total_slots - used_slots

        return cls(
            name=pool_name,
            description=data.get("description") if isinstance(data, dict) else None,
            slots=slots,
            total_slots=total_slots,
            used_slots=used_slots,
            available_slots=available_slots,
        )

    def get_utilization_rate(self) -> float:
        if self.total_slots == 0:
            return 0.0
        return self.used_slots / self.total_slots

    def has_available_slots(self, min_slots: int = 1) -> bool:
        return self.available_slots >= min_slots

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "total_slots": self.total_slots,
            "used_slots": self.used_slots,
            "available_slots": self.available_slots,
            "utilization_rate": self.get_utilization_rate(),
        }


__all__ = ["ResourcePool", "Slot"]

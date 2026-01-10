"""Helpers for inspecting Determined resource pools."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set

from determined_batch.core.api_client import DeterminedAPIClient
from determined_batch.domain.resource_pool import ResourcePool, Slot


class ResourcePoolService:
    def __init__(self, api_client: Optional[DeterminedAPIClient] = None) -> None:
        self.api_client = api_client or DeterminedAPIClient()
        self._pools_cache: Optional[List[ResourcePool]] = None
        self._slots_cache: Optional[List[Slot]] = None

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------
    def get_all_slots(self, force_refresh: bool = False) -> List[Slot]:
        if self._slots_cache is None or force_refresh:
            try:
                slots_data = self.api_client.get_slots()
                self._slots_cache = [Slot.from_api_data(slot) for slot in slots_data]
            except Exception:
                return []
        return self._slots_cache

    def get_all_pools(self, force_refresh: bool = False) -> List[ResourcePool]:
        if self._pools_cache is None or force_refresh:
            try:
                pools_data = self.api_client.get_resource_pools()
                slots = self.get_all_slots(force_refresh=force_refresh)
                self._pools_cache = [ResourcePool.from_api_data(pool, slots=slots) for pool in pools_data]
            except Exception:
                return []
        return self._pools_cache

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------
    def get_available_pools(
        self,
        avoid_pools: Optional[Set[str]] = None,
        min_free_slots: int = 1,
        prefer_pools: Optional[Sequence[str]] = None,
        max_pools: Optional[int] = None,
    ) -> List[ResourcePool]:
        avoid_pools = avoid_pools or set()
        pools = [p for p in self.get_all_pools() if p.name not in avoid_pools and p.available_slots >= min_free_slots]

        def sort_key(pool: ResourcePool) -> tuple:
            preference = len(prefer_pools) if prefer_pools else 0
            if prefer_pools and pool.name in prefer_pools:
                preference = prefer_pools.index(pool.name)
            return (preference, -pool.available_slots, pool.name)

        pools.sort(key=sort_key)
        if max_pools:
            pools = pools[:max_pools]
        return pools

    def select_best_pool(
        self,
        required_slots: int = 1,
        prefer_pools: Optional[Sequence[str]] = None,
        avoid_pools: Optional[Set[str]] = None,
        min_free_slots: int = 1,
    ) -> Optional[ResourcePool]:
        available = self.get_available_pools(
            avoid_pools=avoid_pools,
            min_free_slots=max(required_slots, min_free_slots),
            prefer_pools=prefer_pools,
            max_pools=1,
        )
        return available[0] if available else None

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def get_pool_stats(self) -> Dict[str, Dict[str, float]]:
        stats: Dict[str, Dict[str, float]] = {}
        for pool in self.get_all_pools():
            stats[pool.name] = pool.to_dict()
        return stats


__all__ = ["ResourcePoolService"]

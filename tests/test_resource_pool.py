import pytest

from determined_batch.core.api_client import APIError
from determined_batch.domain.resource_pool import ResourcePool
from determined_batch.domain.resource_pool import Slot
from determined_batch.services.resource_pool_service import ResourcePoolService


class FakeClient:
    def get_resource_pools(self):
        return [
            {"name": "full", "slotsAvailable": 4, "slotsUsed": 4},
            {"name": "free", "slotsAvailable": 8, "slotsUsed": 3},
            {"name": "unknown"},
        ]

    def get_slots(self):
        return []


def test_api_capacity_and_unknown_capacity_are_distinct():
    pools = ResourcePoolService(FakeClient()).get_all_pools()
    by_name = {pool.name: pool for pool in pools}
    assert by_name["full"].capacity_known is True
    assert by_name["full"].available_slots == 0
    assert by_name["free"].available_slots == 5
    # Slot inventory was successfully fetched and empty, so the fallback is known zero.
    assert by_name["unknown"].capacity_known is True
    assert [pool.name for pool in ResourcePoolService(FakeClient()).get_available_pools()] == ["free"]


def test_resource_errors_propagate():
    class Broken(FakeClient):
        def get_resource_pools(self):
            raise APIError("unavailable", code=503, retryable=True)

    with pytest.raises(APIError):
        ResourcePoolService(Broken()).get_all_pools()


def test_absent_capacity_and_slot_inventory_is_unknown():
    pool = ResourcePool.from_api_data({"name": "dynamic"}, slots=None)
    assert pool.capacity_known is False
    assert pool.has_available_slots() is False


def test_pool_keeps_only_its_multi_membership_slot_records():
    slots = [
        Slot(slot_id="0", agent_id="a", resource_pool="p1"),
        Slot(slot_id="0", agent_id="a", resource_pool="p2"),
    ]
    pool = ResourcePool.from_api_data({"name": "p2"}, slots=slots)
    assert [slot.resource_pool for slot in pool.slots] == ["p2"]

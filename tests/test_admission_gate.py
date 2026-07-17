import asyncio
import unittest

from grok_register.core.admission import AdmissionGate


class FakeInventory:
    def __init__(self):
        self.t_depth = 0
        self.q_depth = 0


class AdmissionGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_t_production_blocks_at_high_water_and_wakes_at_low_water(self):
        inventory = FakeInventory()
        gate = AdmissionGate(inventory, t_low=1, t_high=3, q_low=1, q_high=3)

        inventory.t_depth = 3
        blocked = asyncio.create_task(gate.acquire_t_production())
        await asyncio.sleep(0.02)
        self.assertFalse(blocked.done())

        inventory.t_depth = 1
        await gate.notify_changed()
        lease = await asyncio.wait_for(blocked, timeout=1)

        self.assertEqual(gate.t_in_progress, 1)
        await lease.release()
        self.assertEqual(gate.t_in_progress, 0)

    async def test_q_batch_reserves_only_current_demand(self):
        inventory = FakeInventory()
        gate = AdmissionGate(inventory, t_low=1, t_high=3, q_low=2, q_high=5)

        lease = await gate.acquire_q_batch(max_batch=4)

        self.assertEqual(lease.count, 4)
        self.assertEqual(gate.q_inflight, 4)

        second = await gate.acquire_q_batch(max_batch=4)
        self.assertEqual(second.count, 1)
        self.assertEqual(gate.q_inflight, 5)

        blocked = asyncio.create_task(gate.acquire_q_batch(max_batch=4))
        await asyncio.sleep(0.02)
        self.assertFalse(blocked.done())

        await lease.release_all()
        await second.release_all()
        inventory.q_depth = 1
        await gate.notify_changed()
        third = await asyncio.wait_for(blocked, timeout=1)
        self.assertEqual(third.count, 4)
        await third.release_all()

    async def test_q_batch_waits_for_low_water_after_reaching_high_water(self):
        inventory = FakeInventory()
        gate = AdmissionGate(inventory, t_low=1, t_high=3, q_low=2, q_high=5)

        lease = await gate.acquire_q_batch(max_batch=5)
        self.assertEqual(lease.count, 5)
        self.assertEqual(gate.q_inflight, 5)

        await lease.release_one()
        self.assertEqual(gate.q_inflight, 4)

        blocked = asyncio.create_task(gate.acquire_q_batch(max_batch=5))
        await asyncio.sleep(0.02)
        self.assertFalse(blocked.done())

        await lease.release_one()
        await lease.release_one()
        await lease.release_one()

        resumed = await asyncio.wait_for(blocked, timeout=1)
        self.assertEqual(resumed.count, 4)
        await lease.release_all()
        await resumed.release_all()


if __name__ == "__main__":
    unittest.main()

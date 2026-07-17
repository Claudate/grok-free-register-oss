import asyncio
import time
import unittest

from grok_register.core.envelope import ResourceEnvelope
from grok_register.core.inventory import Inventory
from grok_register.core.observer import Metrics


class InventoryUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_with_slot_releases_slot_if_constructor_fails(self):
        class BrokenEnvelope(ResourceEnvelope):
            def __init__(self, *args, **kwargs):
                raise RuntimeError("boom")

        sem = asyncio.Semaphore(1)

        with self.assertRaises(RuntimeError):
            await BrokenEnvelope.create_with_slot("T", "tok", sem)

        self.assertEqual(sem._value, 1)

    async def test_lazy_cleanup_scans_past_fresh_head(self):
        metrics = Metrics()
        inventory = Inventory(metrics=metrics)
        t_sem = asyncio.Semaphore(3)
        q_sem = asyncio.Semaphore(1)
        now = time.time()

        fresh_head = await ResourceEnvelope.create_with_slot(
            "T", "fresh_head", t_sem, expires_at=now + 100
        )
        stale_tail = await ResourceEnvelope.create_with_slot(
            "T", "stale_tail", t_sem, expires_at=now + 100
        )
        fresh_tail = await ResourceEnvelope.create_with_slot(
            "T", "fresh_tail", t_sem, expires_at=now + 100
        )

        await inventory.put_t(fresh_head)
        await inventory.put_t(stale_tail)
        await inventory.put_t(fresh_tail)
        self.assertEqual(t_sem._value, 0)

        stale_tail.expires_at = time.time() - 10

        q_env = await ResourceEnvelope.create_with_slot(
            "Q", "q", q_sem, expires_at=now + 100
        )
        await inventory.put_q(q_env)

        self.assertEqual(t_sem._value, 1)
        self.assertEqual(metrics.t_expired, 1)
        self.assertEqual(inventory.t_depth, 2)


if __name__ == "__main__":
    unittest.main()

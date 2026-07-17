"""
Layer 1: 单元测试

测 ResourceEnvelope、PairLease、Inventory 的局部语义:
  - release_slot_once() 幂等
  - create_with_slot() slot 获取失败不创建 envelope
  - is_expired() 正确判断
  - put_t/put_q 成功前所有权属于 Worker,成功后属于 Inventory
  - claim_pair 等待时取消不弹资源
  - claim_pair 成功后 pair 属于 PairLease
  - PairLease 退出时核销 T/Q
  - 过期资源不会交付给 C
  - lazy cleanup 只在事件触发时发生
  - discard() 幂等释放
"""
import asyncio
import time
import pytest

from grok_register.core.envelope import ResourceEnvelope
from grok_register.core.inventory import Inventory, PairLease


# ═══════════════════════════════════════════
#  ResourceEnvelope 测试
# ═══════════════════════════════════════════
class TestResourceEnvelope:
    """ResourceEnvelope 语义测试。"""

    @pytest.mark.asyncio
    async def test_create_with_slot_acquires_sem(self):
        """create_with_slot 成功后 semaphore 值减 1。"""
        sem = asyncio.Semaphore(3)
        env = await ResourceEnvelope.create_with_slot('T', 'tok1', sem)
        assert sem._value == 2
        assert env.value == 'tok1'
        assert env.kind == 'T'
        assert not env.released

    @pytest.mark.asyncio
    async def test_create_with_slot_fails_no_envelope(self):
        """slot 获取失败(取消)时不创建 envelope,sem 不变。"""
        sem = asyncio.Semaphore(0)
        with pytest.raises(asyncio.CancelledError):
            # 手动取消
            task = asyncio.current_task()
            task.cancel()
            try:
                await ResourceEnvelope.create_with_slot('T', 'tok1', sem)
            except asyncio.CancelledError:
                # sem 不应改变(因为 acquire 被取消了)
                raise
        # sem 仍为 0
        assert sem._value == 0

    @pytest.mark.asyncio
    async def test_release_slot_once_idempotent(self):
        """release_slot_once() 幂等:重复调用只释放一次。"""
        sem = asyncio.Semaphore(3)
        env = await ResourceEnvelope.create_with_slot('T', 'tok1', sem)
        assert sem._value == 2

        env.release_slot_once(reason='test1')
        assert sem._value == 3
        assert env.released

        # 第二次调用不应再次释放
        env.release_slot_once(reason='test2')
        assert sem._value == 3

    @pytest.mark.asyncio
    async def test_discard_releases_slot(self):
        """discard() 释放 slot。"""
        sem = asyncio.Semaphore(2)
        env = await ResourceEnvelope.create_with_slot('Q', {'code': '123'}, sem)
        assert sem._value == 1
        env.discard(reason='test')
        assert sem._value == 2
        assert env.released

    @pytest.mark.asyncio
    async def test_discard_idempotent(self):
        """discard() 幂等。"""
        sem = asyncio.Semaphore(2)
        env = await ResourceEnvelope.create_with_slot('Q', 'val', sem)
        env.discard(reason='first')
        env.discard(reason='second')
        assert sem._value == 2

    @pytest.mark.asyncio
    async def test_is_expired_with_expires_at(self):
        """有 expires_at 时正确判断过期。"""
        sem = asyncio.Semaphore(2)
        now = time.time()
        env = await ResourceEnvelope.create_with_slot('T', 'tok', sem, expires_at=now - 1)
        assert env.is_expired(now=now)

        env2 = await ResourceEnvelope.create_with_slot('T', 'tok2', sem, expires_at=now + 100)
        assert not env2.is_expired(now=now)

    @pytest.mark.asyncio
    async def test_is_expired_without_expires_at(self):
        """没有 expires_at 时永远不过期。"""
        sem = asyncio.Semaphore(2)
        env = await ResourceEnvelope.create_with_slot('T', 'tok', sem)
        assert not env.is_expired(now=time.time() + 99999)

    @pytest.mark.asyncio
    async def test_create_with_slot_meta(self):
        """meta 字段正确传递。"""
        sem = asyncio.Semaphore(1)
        env = await ResourceEnvelope.create_with_slot('T', 'tok', sem, meta={'source': 'test'})
        assert env.meta == {'source': 'test'}


# ═══════════════════════════════════════════
#  Inventory 测试
# ═══════════════════════════════════════════
class TestInventory:
    """Inventory 语义测试。"""

    @pytest.mark.asyncio
    async def test_put_t_then_claim(self):
        """put_t 后 claim_pair 能拿到 pair。"""
        inv = Inventory()
        t_sem = asyncio.Semaphore(2)
        q_sem = asyncio.Semaphore(2)

        t_env = await ResourceEnvelope.create_with_slot('T', 'token1', t_sem)
        q_env = await ResourceEnvelope.create_with_slot('Q', {'code': 'abc'}, q_sem)

        await inv.put_t(t_env)
        await inv.put_q(q_env)

        assert inv.t_depth == 1
        assert inv.q_depth == 1

        # claim
        async with inv.claim_pair() as pair:
            assert pair.t.value == 'token1'
            assert pair.q.value == {'code': 'abc'}
            # claim 后 inventory 应为空
            assert inv.t_depth == 0
            assert inv.q_depth == 0

        # PairLease 退出后 slot 应被释放
        assert t_sem._value == 2
        assert q_sem._value == 2

    @pytest.mark.asyncio
    async def test_claim_waits_for_both(self):
        """只有 T 没有 Q 时,claim_pair 应等待。"""
        inv = Inventory()
        t_sem = asyncio.Semaphore(2)
        q_sem = asyncio.Semaphore(2)

        t_env = await ResourceEnvelope.create_with_slot('T', 'tok', t_sem)
        await inv.put_t(t_env)

        claimed = False

        async def claimer():
            nonlocal claimed
            async with inv.claim_pair() as pair:
                claimed = True

        task = asyncio.create_task(claimer())
        await asyncio.sleep(0.1)
        assert not claimed, 'claim 不应在缺少 Q 时成功'

        # 现在放入 Q
        q_env = await ResourceEnvelope.create_with_slot('Q', {'code': 'x'}, q_sem)
        await inv.put_q(q_env)

        await asyncio.wait_for(task, timeout=2)
        assert claimed

    @pytest.mark.asyncio
    async def test_claim_cancel_no_pop(self):
        """claim_pair 等待时取消,不应弹出任何资源。"""
        inv = Inventory()
        t_sem = asyncio.Semaphore(2)

        t_env = await ResourceEnvelope.create_with_slot('T', 'tok', t_sem)
        await inv.put_t(t_env)
        assert inv.t_depth == 1

        async def claimer():
            async with inv.claim_pair() as pair:
                pass  # 不应到达

        task = asyncio.create_task(claimer())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # T 仍在 inventory 中
        assert inv.t_depth == 1

    @pytest.mark.asyncio
    async def test_pair_lease_settles_on_success(self):
        """PairLease 成功退出时核销 T/Q 并释放 slot。"""
        inv = Inventory()
        t_sem = asyncio.Semaphore(2)
        q_sem = asyncio.Semaphore(2)

        t_env = await ResourceEnvelope.create_with_slot('T', 'tok', t_sem)
        q_env = await ResourceEnvelope.create_with_slot('Q', {'code': 'x'}, q_sem)
        await inv.put_t(t_env)
        await inv.put_q(q_env)

        async with inv.claim_pair() as pair:
            # slot 还在 pair 中
            assert t_sem._value == 1
            assert q_sem._value == 1

        # 退出后 slot 释放
        assert t_sem._value == 2
        assert q_sem._value == 2

    @pytest.mark.asyncio
    async def test_pair_lease_settles_on_exception(self):
        """PairLease 异常退出时也核销 T/Q。"""
        inv = Inventory()
        t_sem = asyncio.Semaphore(2)
        q_sem = asyncio.Semaphore(2)

        t_env = await ResourceEnvelope.create_with_slot('T', 'tok', t_sem)
        q_env = await ResourceEnvelope.create_with_slot('Q', {'code': 'x'}, q_sem)
        await inv.put_t(t_env)
        await inv.put_q(q_env)

        with pytest.raises(ValueError):
            async with inv.claim_pair() as pair:
                raise ValueError('boom')

        # slot 仍被核销
        assert t_sem._value == 2
        assert q_sem._value == 2

    @pytest.mark.asyncio
    async def test_pair_lease_settles_on_cancel(self):
        """PairLease 取消退出时也核销 T/Q。"""
        inv = Inventory()
        t_sem = asyncio.Semaphore(2)
        q_sem = asyncio.Semaphore(2)

        t_env = await ResourceEnvelope.create_with_slot('T', 'tok', t_sem)
        q_env = await ResourceEnvelope.create_with_slot('Q', {'code': 'x'}, q_sem)
        await inv.put_t(t_env)
        await inv.put_q(q_env)

        async def consumer():
            async with inv.claim_pair() as pair:
                # 在 pair 内部被取消
                await asyncio.sleep(999)

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # slot 仍被核销
        assert t_sem._value == 2
        assert q_sem._value == 2

    @pytest.mark.asyncio
    async def test_expired_t_not_delivered(self):
        """过期的 T 不会交付给 C。"""
        inv = Inventory()
        t_sem = asyncio.Semaphore(2)
        q_sem = asyncio.Semaphore(2)

        now = time.time()
        # T 已过期
        t_env = await ResourceEnvelope.create_with_slot('T', 'expired_tok', t_sem, expires_at=now - 10)
        # Q 有效
        q_env = await ResourceEnvelope.create_with_slot('Q', {'code': 'x'}, q_sem, expires_at=now + 100)

        await inv.put_t(t_env)
        await inv.put_q(q_env)

        # claim 应该把过期的 T 清理掉,然后等待新的 T
        claimed = False

        async def claimer():
            nonlocal claimed
            async with inv.claim_pair() as pair:
                claimed = True

        task = asyncio.create_task(claimer())
        await asyncio.sleep(0.2)
        assert not claimed, '过期 T 不应交付'
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # 过期 T 被清理,slot 被释放
        assert t_sem._value == 2
        # Q 仍在 inventory 中
        assert inv.q_depth == 1

    @pytest.mark.asyncio
    async def test_expired_q_not_delivered(self):
        """过期的 Q 不会交付给 C。"""
        inv = Inventory()
        t_sem = asyncio.Semaphore(2)
        q_sem = asyncio.Semaphore(2)

        now = time.time()
        t_env = await ResourceEnvelope.create_with_slot('T', 'tok', t_sem, expires_at=now + 100)
        q_env = await ResourceEnvelope.create_with_slot('Q', {'code': 'x'}, q_sem, expires_at=now - 10)

        await inv.put_t(t_env)
        await inv.put_q(q_env)

        claimed = False

        async def claimer():
            nonlocal claimed
            async with inv.claim_pair() as pair:
                claimed = True

        task = asyncio.create_task(claimer())
        await asyncio.sleep(0.2)
        assert not claimed, '过期 Q 不应交付'
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert q_sem._value == 2
        assert inv.t_depth == 1

    @pytest.mark.asyncio
    async def test_lazy_cleanup_on_put(self):
        """lazy cleanup: put 时清理已过期的库存。"""
        inv = Inventory()
        t_sem = asyncio.Semaphore(4)

        now = time.time()
        # 放入 3 个过期的 T
        for i in range(3):
            env = await ResourceEnvelope.create_with_slot(
                'T', f'expired_{i}', t_sem, expires_at=now - 10
            )
            await inv.put_t(env)

        # 由于 put 时 lazy cleanup 已清理过期项,库存应为空
        assert inv.t_depth == 0
        # slot 应已释放
        assert t_sem._value == 4

    @pytest.mark.asyncio
    async def test_lazy_cleanup_silent_until_next_event(self):
        """lazy cleanup 不在静默期主动释放,下一次 put 才触发清理。"""
        inv = Inventory()
        t_sem = asyncio.Semaphore(4)

        env = await ResourceEnvelope.create_with_slot(
            'T', 'will_expire', t_sem, expires_at=time.time() + 0.05
        )
        await inv.put_t(env)

        await asyncio.sleep(0.1)
        assert inv.t_depth == 1
        assert t_sem._value == 3

        fresh = await ResourceEnvelope.create_with_slot(
            'T', 'fresh', t_sem, expires_at=time.time() + 100
        )
        await inv.put_t(fresh)

        assert inv.t_depth == 1
        assert t_sem._value == 3

    @pytest.mark.asyncio
    async def test_lazy_cleanup_on_claim(self):
        """lazy cleanup: claim_pair 时清理过期库存。"""
        inv = Inventory()
        t_sem = asyncio.Semaphore(4)
        q_sem = asyncio.Semaphore(4)

        now = time.time()
        # 直接注入过期项到 inventory 内部(绕过 put 的清理)
        env = await ResourceEnvelope.create_with_slot('T', 'expired', t_sem, expires_at=now - 10)
        inv._t_buf.append(env)

        # 有效的 Q
        q_env = await ResourceEnvelope.create_with_slot('Q', {'code': 'x'}, q_sem, expires_at=now + 100)
        await inv.put_q(q_env)

        # claim 时应清理过期的 T
        claimed = False
        async def claimer():
            nonlocal claimed
            async with inv.claim_pair() as pair:
                claimed = True

        task = asyncio.create_task(claimer())
        await asyncio.sleep(0.2)
        assert not claimed  # T 被清了,没有 pair
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert inv.t_depth == 0
        assert t_sem._value == 4  # slot 已释放

    @pytest.mark.asyncio
    async def test_fifo_order(self):
        """Inventory 按 FIFO 顺序配对。"""
        inv = Inventory()
        t_sem = asyncio.Semaphore(10)
        q_sem = asyncio.Semaphore(10)

        for i in range(3):
            t_env = await ResourceEnvelope.create_with_slot('T', f'tok_{i}', t_sem)
            q_env = await ResourceEnvelope.create_with_slot('Q', {'code': f'c_{i}'}, q_sem)
            await inv.put_t(t_env)
            await inv.put_q(q_env)

        pairs = []
        for _ in range(3):
            async with inv.claim_pair() as pair:
                pairs.append((pair.t.value, pair.q.value['code']))

        assert pairs == [('tok_0', 'c_0'), ('tok_1', 'c_1'), ('tok_2', 'c_2')]

    @pytest.mark.asyncio
    async def test_multiple_concurrent_claims(self):
        """多个 C 同时 claim,不应重复消费同一个 T/Q。"""
        inv = Inventory()
        t_sem = asyncio.Semaphore(10)
        q_sem = asyncio.Semaphore(10)

        n = 5
        for i in range(n):
            t_env = await ResourceEnvelope.create_with_slot('T', f'tok_{i}', t_sem)
            q_env = await ResourceEnvelope.create_with_slot('Q', {'code': f'c_{i}'}, q_sem)
            await inv.put_t(t_env)
            await inv.put_q(q_env)

        results = []

        async def claimer(idx):
            async with inv.claim_pair() as pair:
                results.append((idx, pair.t.value))

        tasks = [asyncio.create_task(claimer(i)) for i in range(n)]
        await asyncio.gather(*tasks)

        # 每个 T 只被消费一次
        t_values = [r[1] for r in results]
        assert len(set(t_values)) == n, f'重复消费: {t_values}'

    @pytest.mark.asyncio
    async def test_metrics_admitted(self):
        """put 成功时 metrics.t_admitted / q_admitted 递增。"""
        from grok_register.core.observer import Metrics
        m = Metrics()
        inv = Inventory(metrics=m)
        t_sem = asyncio.Semaphore(2)

        env = await ResourceEnvelope.create_with_slot('T', 'tok', t_sem)
        await inv.put_t(env)
        assert m.t_admitted == 1

    @pytest.mark.asyncio
    async def test_metrics_expired(self):
        """过期资源 put 时 metrics.t_expired 递增。"""
        from grok_register.core.observer import Metrics
        m = Metrics()
        inv = Inventory(metrics=m)
        t_sem = asyncio.Semaphore(2)

        env = await ResourceEnvelope.create_with_slot('T', 'tok', t_sem, expires_at=time.time() - 10)
        await inv.put_t(env)
        assert m.t_expired == 1
        assert m.t_admitted == 0

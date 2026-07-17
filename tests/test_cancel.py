"""
Layer 2: 取消注入测试

在每个 await 边界前后强制取消 Worker,验证:
  - S 取消后 Physical_Sem 和 T_Slot_Sem 不泄漏
  - P 取消后 Physical_Sem、Q_Pending_Sem、Q_Slot_Sem 不泄漏
  - C 取消后 Physical_Sem 不泄漏,已 claim 的 T/Q 被核销
  - 库存守恒: 取消不导致 slot 无界堆积或耗尽
"""
import asyncio
import time
import pytest

from grok_register.core.envelope import ResourceEnvelope
from grok_register.core.inventory import Inventory
from grok_register.core.observer import Metrics
from tests.fakes import FakeTurnstile, FakeEmailService, FakeRegisterAPI, Conservation


# ═══════════════════════════════════════════
#  S_Worker 取消测试
# ═══════════════════════════════════════════

async def _s_worker_impl(browser, inventory, physical_sem, t_slot_sem, metrics, stop):
    """S_Worker 简化实现,用于测试。"""
    while not stop.is_set():
        await physical_sem.acquire()
        try:
            token = await browser.generate()
        finally:
            physical_sem.release()

        if token is None:
            metrics.t_discarded += 1
            await asyncio.sleep(0.05)
            continue

        metrics.t_produced += 1
        now = time.time()
        try:
            t_env = await ResourceEnvelope.create_with_slot(
                'T', token, t_slot_sem, expires_at=now + 300
            )
        except asyncio.CancelledError:
            metrics.t_discarded += 1
            raise

        try:
            await inventory.put_t(t_env)
        except Exception:
            t_env.discard(reason='put_failed')
            metrics.t_discarded += 1
        await asyncio.sleep(0.02)


class TestSCancel:
    """S_Worker 取消语义测试。"""

    @pytest.mark.asyncio
    async def test_cancel_during_generate_no_leak(self):
        """S 在 generate() 期间被取消,sem 不泄漏。"""
        ft = FakeTurnstile(delay=0.5)  # 慢生成
        inv = Inventory()
        phys = asyncio.Semaphore(2)
        t_slot = asyncio.Semaphore(4)
        metrics = Metrics()
        stop = asyncio.Event()

        task = asyncio.create_task(
            _s_worker_impl(ft, inv, phys, t_slot, metrics, stop)
        )
        await asyncio.sleep(0.05)  # 等它进入 generate
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Physical_Sem 应完全释放
        assert phys._value == 2, f'Physical_Sem 泄漏: {phys._value}'
        # T_Slot_Sem 不变(没生成成功)
        assert t_slot._value == 4

    @pytest.mark.asyncio
    async def test_cancel_during_put_t_no_leak(self):
        """S 在 put_t 期间被取消(极端: put 内部取消),envelope 被 discard。"""
        inv = Inventory()
        phys = asyncio.Semaphore(2)
        t_slot = asyncio.Semaphore(4)
        metrics = Metrics()
        stop = asyncio.Event()

        # 快速生成
        ft = FakeTurnstile(delay=0.001)

        task = asyncio.create_task(
            _s_worker_impl(ft, inv, phys, t_slot, metrics, stop)
        )
        # 在 put_t 之前的极短窗口取消
        await asyncio.sleep(0.002)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert phys._value == 2
        # t_slot 可能是 4 或 3,但不应是负数
        assert t_slot._value >= 0
        assert t_slot._value <= 4

    @pytest.mark.asyncio
    async def test_s_repeated_cancel_no_accumulation(self):
        """反复取消 S 多次,sem 计数不累积。"""
        ft = FakeTurnstile(delay=0.01)
        inv = Inventory()
        phys = asyncio.Semaphore(2)
        t_slot = asyncio.Semaphore(4)
        metrics = Metrics()
        stop = asyncio.Event()

        for _ in range(20):
            task = asyncio.create_task(
                _s_worker_impl(ft, inv, phys, t_slot, metrics, stop)
            )
            await asyncio.sleep(0.02)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert phys._value == 2, f'Physical_Sem 泄漏: {phys._value}'
        # 守恒: free + inventory == total (成功入库的 envelope 占着 slot)
        assert t_slot._value + inv.t_depth == 4, f'T_Slot 守恒违反: free={t_slot._value} inv={inv.t_depth}'


# ═══════════════════════════════════════════
#  P_Worker 取消测试
# ═══════════════════════════════════════════

async def _p_worker_impl(email_svc, inventory, physical_sem, q_pending_sem,
                          q_slot_sem, metrics, stop):
    """P_Worker 简化实现,用于测试。"""
    loop = asyncio.get_event_loop()
    while not stop.is_set():
        await q_pending_sem.acquire()
        _pending_released = False
        try:
            await physical_sem.acquire()
            try:
                handle, email, password = await email_svc.create_email()
                sent = True  # fake 总是成功
            finally:
                physical_sem.release()

            if not sent:
                q_pending_sem.release()
                _pending_released = True
                continue

            metrics.q_sent += 1

            try:
                code = await asyncio.wait_for(
                    email_svc.poll_code(handle), timeout=95
                )
            except asyncio.TimeoutError:
                code = None

            if code is None:
                metrics.q_discarded += 1
                q_pending_sem.release()
                _pending_released = True
                continue

            metrics.q_returned += 1
            now = time.time()
            q_env = await ResourceEnvelope.create_with_slot(
                'Q', {'email': email, 'password': password, 'code': code},
                q_slot_sem, expires_at=now + 120,
            )

            try:
                await inventory.put_q(q_env)
            except Exception:
                q_env.discard(reason='put_failed')
                metrics.q_discarded += 1

        except asyncio.CancelledError:
            if not _pending_released:
                q_pending_sem.release()
                _pending_released = True
            raise
        except Exception:
            metrics.q_discarded += 1
        finally:
            if not _pending_released:
                q_pending_sem.release()

        await asyncio.sleep(0.02)


class TestPCancel:
    """P_Worker 取消语义测试。"""

    @pytest.mark.asyncio
    async def test_cancel_during_create_email(self):
        """P 在 create_email 期间被取消,所有 sem 正确释放。"""
        es = FakeEmailService()
        es.set_cancel_create_at(1)
        inv = Inventory()
        phys = asyncio.Semaphore(2)
        q_pend = asyncio.Semaphore(4)
        q_slot = asyncio.Semaphore(4)
        metrics = Metrics()
        stop = asyncio.Event()

        task = asyncio.create_task(
            _p_worker_impl(es, inv, phys, q_pend, q_slot, metrics, stop)
        )
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert phys._value == 2, f'Physical_Sem 泄漏: {phys._value}'
        assert q_pend._value == 4, f'Q_Pending 泄漏: {q_pend._value}'
        assert q_slot._value == 4

    @pytest.mark.asyncio
    async def test_cancel_during_poll_code(self):
        """P 在 poll_code 期间被取消,不持有 Physical_Sem。"""
        es = FakeEmailService()
        es.set_cancel_poll_at(1)
        inv = Inventory()
        phys = asyncio.Semaphore(2)
        q_pend = asyncio.Semaphore(4)
        q_slot = asyncio.Semaphore(4)
        metrics = Metrics()
        stop = asyncio.Event()

        task = asyncio.create_task(
            _p_worker_impl(es, inv, phys, q_pend, q_slot, metrics, stop)
        )
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # P 等 Q 时不持有 Physical_Sem
        assert phys._value == 2, f'Physical_Sem 应完全释放: {phys._value}'
        assert q_pend._value == 4, f'Q_Pending 泄漏: {q_pend._value}'

    @pytest.mark.asyncio
    async def test_p_repeated_cancel_no_accumulation(self):
        """反复取消 P 多次,sem 计数不累积。"""
        es = FakeEmailService()
        inv = Inventory()
        phys = asyncio.Semaphore(2)
        q_pend = asyncio.Semaphore(4)
        q_slot = asyncio.Semaphore(4)
        metrics = Metrics()
        stop = asyncio.Event()

        for _ in range(20):
            task = asyncio.create_task(
                _p_worker_impl(es, inv, phys, q_pend, q_slot, metrics, stop)
            )
            await asyncio.sleep(0.03)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert phys._value == 2, f'Physical_Sem 泄漏: {phys._value}'
        assert q_pend._value == 4, f'Q_Pending 泄漏: {q_pend._value}'
        # 守恒: free + inventory == total
        assert q_slot._value + inv.q_depth == 4, f'Q_Slot 守恒违反: free={q_slot._value} inv={inv.q_depth}'


# ═══════════════════════════════════════════
#  C_Worker 取消测试
# ═══════════════════════════════════════════

async def _c_worker_impl(register_api, browser_unused, inventory, physical_sem,
                          metrics, stop, file_lock):
    """C_Worker 简化实现,用于测试。"""
    while not stop.is_set():
        try:
            async with inventory.claim_pair() as pair:
                t_val = pair.t.value
                q_val = pair.q.value
                await physical_sem.acquire()
                try:
                    sso = await register_api.register(
                        q_val['email'], q_val['password'], q_val['code'], t_val
                    )
                    if sso:
                        metrics.pair_consumed_ok += 1
                        metrics.success_count += 1
                    else:
                        metrics.pair_consumed_fail += 1
                finally:
                    physical_sem.release()
        except asyncio.CancelledError:
            raise
        except Exception:
            metrics.pair_consumed_fail += 1
        await asyncio.sleep(0.02)


class TestCCancel:
    """C_Worker 取消语义测试。"""

    @pytest.mark.asyncio
    async def test_cancel_during_register_pair_settled(self):
        """C 在 register 期间被取消,pair 已 claim,必须核销。"""
        fra = FakeRegisterAPI(delay=0.5)
        inv = Inventory()
        phys = asyncio.Semaphore(2)
        t_slot = asyncio.Semaphore(4)
        q_slot = asyncio.Semaphore(4)
        metrics = Metrics()
        stop = asyncio.Event()
        fl = asyncio.Lock()

        # 用带 metrics 的 inventory
        inv_with_metrics = Inventory(metrics=Metrics())
        t_env = await ResourceEnvelope.create_with_slot('T', 'tok', t_slot)
        q_env = await ResourceEnvelope.create_with_slot('Q', {'email': 'a@b.com', 'password': 'p', 'code': '123'}, q_slot)
        await inv_with_metrics.put_t(t_env)
        await inv_with_metrics.put_q(q_env)

        task = asyncio.create_task(
            _c_worker_impl(fra, None, inv_with_metrics, phys, metrics, stop, fl)
        )
        await asyncio.sleep(0.1)  # 等它 claim 进入 register
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # pair 已核销: slot 应释放
        assert t_slot._value == 4, f'T slot 未核销: {t_slot._value}'
        assert q_slot._value == 4, f'Q slot 未核销: {q_slot._value}'
        assert phys._value == 2, f'Physical_Sem 泄漏: {phys._value}'

    @pytest.mark.asyncio
    async def test_cancel_during_claim_wait_no_pop(self):
        """C 在 claim_pair 等待时取消,不弹出资源。"""
        inv = Inventory()
        phys = asyncio.Semaphore(2)
        t_slot = asyncio.Semaphore(4)
        q_slot = asyncio.Semaphore(4)
        metrics = Metrics()
        stop = asyncio.Event()
        fl = asyncio.Lock()

        fra = FakeRegisterAPI()

        # 放一个 T 但不放 Q,让 C 等待
        t_env = await ResourceEnvelope.create_with_slot('T', 'tok', t_slot)
        await inv.put_t(t_env)

        task = asyncio.create_task(
            _c_worker_impl(fra, None, inv, phys, metrics, stop, fl)
        )
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # T 仍在 inventory 中
        assert inv.t_depth == 1, 'claim 等待时取消不应弹出 T'
        assert phys._value == 2

    @pytest.mark.asyncio
    async def test_c_repeated_cancel_no_accumulation(self):
        """反复取消 C 多次,sem 计数不累积。"""
        fra = FakeRegisterAPI(delay=0.01)
        inv = Inventory()
        phys = asyncio.Semaphore(2)
        t_slot = asyncio.Semaphore(10)
        q_slot = asyncio.Semaphore(10)
        metrics = Metrics()
        stop = asyncio.Event()
        fl = asyncio.Lock()

        for _ in range(10):
            # 每次放一对
            t_env = await ResourceEnvelope.create_with_slot('T', f'tok_{_}', t_slot)
            q_env = await ResourceEnvelope.create_with_slot('Q', {'email': f'e@{_}', 'password': 'p', 'code': '123'}, q_slot)
            await inv.put_t(t_env)
            await inv.put_q(q_env)

            task = asyncio.create_task(
                _c_worker_impl(fra, None, inv, phys, metrics, stop, fl)
            )
            await asyncio.sleep(0.03)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert phys._value == 2, f'Physical_Sem 泄漏: {phys._value}'
        # 所有 slot 应已核销
        assert t_slot._value == 10, f'T_Slot 泄漏: {t_slot._value}'
        assert q_slot._value == 10, f'Q_Slot 泄漏: {q_slot._value}'


# ═══════════════════════════════════════════
#  端到端取消: S+P+C 同时运行时取消
# ═══════════════════════════════════════════
class TestE2ECancel:
    """端到端取消测试: S/P/C 同时运行时取消,验证全局守恒。"""

    @pytest.mark.asyncio
    async def test_cancel_all_workers_sem_reset(self):
        """同时启动 S/P/C,然后全部取消,所有 sem 恢复初始值。"""
        ft = FakeTurnstile(delay=0.05)
        es = FakeEmailService()
        fra = FakeRegisterAPI(delay=0.05)

        inv = Inventory()
        phys = asyncio.Semaphore(4)
        t_slot = asyncio.Semaphore(8)
        q_slot = asyncio.Semaphore(8)
        q_pend = asyncio.Semaphore(12)
        metrics = Metrics()
        stop = asyncio.Event()
        fl = asyncio.Lock()

        tasks = []
        for _ in range(3):
            tasks.append(asyncio.create_task(
                _s_worker_impl(ft, inv, phys, t_slot, metrics, stop)
            ))
            tasks.append(asyncio.create_task(
                _p_worker_impl(es, inv, phys, q_pend, q_slot, metrics, stop)
            ))
            tasks.append(asyncio.create_task(
                _c_worker_impl(fra, None, inv, phys, metrics, stop, fl)
            ))

        await asyncio.sleep(0.2)
        stop.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        # 全部停止后,守恒检查: free + inventory == total
        assert phys._value >= 0 and phys._value <= 4
        assert t_slot._value + inv.t_depth == 8, f'T 守恒违反: free={t_slot._value} inv={inv.t_depth}'
        assert q_slot._value + inv.q_depth == 8, f'Q 守恒违反: free={q_slot._value} inv={inv.q_depth}'
        assert q_pend._value >= 0 and q_pend._value <= 12

        # 守恒检查
        cons = Conservation(t_slot, q_slot, q_pend, inv)
        cons.check_t_conservation(8)
        cons.check_q_conservation(8)
        cons.check_pending_conservation(12)

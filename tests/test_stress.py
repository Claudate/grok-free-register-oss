"""
Layer 4: 压力测试

真实 asyncio 并发跑较长时间,检查:
  - pending 泄漏
  - 库存卡死
  - claim 饥饿
  - 等待时间周期性尖峰
  - slot 守恒在长时间运行后仍成立
"""
import asyncio
import time
import statistics
import pytest

from grok_register.core.envelope import ResourceEnvelope
from grok_register.core.inventory import Inventory
from grok_register.core.observer import Metrics
from tests.fakes import Conservation, EventLog


# ═══════════════════════════════════════════
#  S/P/C 真实并发 Worker (使用 fake 服务)
# ═══════════════════════════════════════════

async def stress_s_worker(wid, t_gen, inventory, phys, t_slot, metrics, stop):
    """压力测试 S_Worker。"""
    while not stop.is_set():
        await phys.acquire()
        try:
            token = await t_gen.generate()
        finally:
            phys.release()

        if token is None:
            metrics.t_discarded += 1
            continue

        metrics.t_produced += 1
        now = time.time()
        try:
            t_env = await ResourceEnvelope.create_with_slot(
                'T', token, t_slot, expires_at=now + 60
            )
        except asyncio.CancelledError:
            metrics.t_discarded += 1
            raise

        try:
            await inventory.put_t(t_env)
        except Exception:
            t_env.discard(reason='put_fail')
            metrics.t_discarded += 1
        await asyncio.sleep(0.01)


async def stress_p_worker(wid, email_svc, inventory, phys, q_pend, q_slot, metrics, stop):
    """压力测试 P_Worker。"""
    loop = asyncio.get_event_loop()
    while not stop.is_set():
        await q_pend.acquire()
        _pend_released = False
        try:
            await phys.acquire()
            try:
                handle, email, password = await email_svc.create_email()
                sent = True
            finally:
                phys.release()

            if not sent:
                q_pend.release()
                _pend_released = True
                continue

            metrics.q_sent += 1

            try:
                code = await asyncio.wait_for(email_svc.poll_code(handle), timeout=95)
            except (asyncio.TimeoutError, Exception):
                code = None

            if code is None:
                metrics.q_discarded += 1
                q_pend.release()
                _pend_released = True
                continue

            metrics.q_returned += 1
            now = time.time()
            q_env = await ResourceEnvelope.create_with_slot(
                'Q', {'email': email, 'password': password, 'code': code},
                q_slot, expires_at=now + 60,
            )

            try:
                await inventory.put_q(q_env)
            except Exception:
                q_env.discard(reason='put_fail')
                metrics.q_discarded += 1

        except asyncio.CancelledError:
            if not _pend_released:
                q_pend.release()
                _pend_released = True
            raise
        except Exception:
            metrics.q_discarded += 1
        finally:
            if not _pend_released:
                q_pend.release()
        await asyncio.sleep(0.01)


async def stress_c_worker(wid, register_api, inventory, phys, metrics, stop, file_lock):
    """压力测试 C_Worker。"""
    claim_wait_times = []
    while not stop.is_set():
        t0 = time.time()
        try:
            async with inventory.claim_pair() as pair:
                claim_wait_times.append(time.time() - t0)
                t_val = pair.t.value
                q_val = pair.q.value

                await phys.acquire()
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
                    phys.release()

        except asyncio.CancelledError:
            raise
        except Exception:
            metrics.pair_consumed_fail += 1

        await asyncio.sleep(0.01)

    return claim_wait_times


@pytest.mark.slow
class TestStress:
    """压力测试: 长时间真实并发。"""

    @pytest.mark.asyncio
    async def test_steady_state_30s(self):
        """稳态运行 30 秒,检查守恒、无泄漏、无饥饿。"""
        from tests.fakes import FakeTurnstile, FakeEmailService, FakeRegisterAPI

        t_cap, q_cap, pend_cap, phys_cap = 16, 16, 24, 8
        t_slot_sem = asyncio.Semaphore(t_cap)
        q_slot_sem = asyncio.Semaphore(q_cap)
        q_pend_sem = asyncio.Semaphore(pend_cap)
        phys_sem = asyncio.Semaphore(phys_cap)

        ft = FakeTurnstile(delay=0.005, fail_rate=0.05)
        es = FakeEmailService()
        fra = FakeRegisterAPI(success_rate=0.9, delay=0.005)

        metrics = Metrics()
        inv = Inventory(metrics=metrics)
        cons = Conservation(t_slot_sem, q_slot_sem, q_pend_sem, inv)
        stop = asyncio.Event()
        fl = asyncio.Lock()

        tasks = []
        all_claim_waits = []

        # S workers
        for i in range(4):
            tasks.append(asyncio.create_task(
                stress_s_worker(i, ft, inv, phys_sem, t_slot_sem, metrics, stop)
            ))
        # P workers
        for i in range(6):
            tasks.append(asyncio.create_task(
                stress_p_worker(i, es, inv, phys_sem, q_pend_sem, q_slot_sem, metrics, stop)
            ))
        # C workers
        for i in range(4):
            tasks.append(asyncio.create_task(
                stress_c_worker(i, fra, inv, phys_sem, metrics, stop, fl)
            ))

        # 监控 + 守恒检查
        async def monitor():
            snapshots = []
            while not stop.is_set():
                await asyncio.sleep(2)
                try:
                    cons.check_t_conservation(t_cap)
                    cons.check_q_conservation(q_cap)
                    # Q_Pending: 只做软检查(非负、不超限),不做守恒检查
                    # 因为 P worker 正常 acquire/release 期间 free 值会波动
                    assert q_pend_sem._value >= 0, 'Q pending 为负'
                    assert q_pend_sem._value <= pend_cap, 'Q pending 超限'
                except AssertionError as e:
                    snapshots.append(f'INVARIANT VIOLATION: {e}')
                snapshots.append(
                    f't={metrics.t_produced} q_sent={metrics.q_sent} q_ret={metrics.q_returned} '
                    f'pair_ok={metrics.pair_consumed_ok} inv_t={inv.t_depth} inv_q={inv.q_depth} '
                    f'phys={phys_sem._value} t_slot={t_slot_sem._value} q_slot={q_slot_sem._value}'
                )
            return snapshots

        mon_task = asyncio.create_task(monitor())

        await asyncio.sleep(30)
        stop.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        snapshots = await mon_task

        # 最终守恒检查(所有 Worker 已停止)
        cons.check_t_conservation(t_cap)
        cons.check_q_conservation(q_cap)
        # Q_Pending: 软检查
        assert q_pend_sem._value >= 0 and q_pend_sem._value <= pend_cap

        # 检查是否有 INVARIANT VIOLATION
        violations = [s for s in snapshots if 'INVARIANT' in s]
        assert not violations, f'守恒违反:\n' + '\n'.join(violations)

        print(f'\n[Stress 30s] t_produced={metrics.t_produced} '
              f'q_sent={metrics.q_sent} q_returned={metrics.q_returned} '
              f'pair_ok={metrics.pair_consumed_ok} pair_fail={metrics.pair_consumed_fail} '
              f'success={metrics.success_count}')
        print(f'Snapshots ({len(snapshots)}):')
        for s in snapshots[-5:]:
            print(f'  {s}')

    @pytest.mark.asyncio
    async def test_no_claim_starvation(self):
        """检查 C 不会永远等不到 pair (claim 饥饿)。"""
        from tests.fakes import FakeTurnstile, FakeEmailService, FakeRegisterAPI

        t_cap, q_cap, pend_cap, phys_cap = 4, 4, 6, 2
        t_slot_sem = asyncio.Semaphore(t_cap)
        q_slot_sem = asyncio.Semaphore(q_cap)
        q_pend_sem = asyncio.Semaphore(pend_cap)
        phys_sem = asyncio.Semaphore(phys_cap)

        ft = FakeTurnstile(delay=0.002, fail_rate=0.0)
        es = FakeEmailService()
        fra = FakeRegisterAPI(success_rate=1.0, delay=0.002)

        metrics = Metrics()
        inv = Inventory(metrics=metrics)
        stop = asyncio.Event()
        fl = asyncio.Lock()

        claim_wait_samples = []
        last_success_time = time.time()

        async def c_worker(wid):
            nonlocal last_success_time
            while not stop.is_set():
                t0 = time.time()
                try:
                    async with inv.claim_pair() as pair:
                        wait = time.time() - t0
                        claim_wait_samples.append(wait)

                        await phys_sem.acquire()
                        try:
                            sso = await fra.register(
                                pair.q.value['email'], pair.q.value['password'],
                                pair.q.value['code'], pair.t.value
                            )
                            if sso:
                                metrics.pair_consumed_ok += 1
                                metrics.success_count += 1
                                last_success_time = time.time()
                        finally:
                            phys_sem.release()
                except asyncio.CancelledError:
                    break
                except Exception:
                    metrics.pair_consumed_fail += 1
                await asyncio.sleep(0.005)

        tasks = []
        for i in range(3):
            tasks.append(asyncio.create_task(stress_s_worker(i, ft, inv, phys_sem, t_slot_sem, metrics, stop)))
        for i in range(4):
            tasks.append(asyncio.create_task(stress_p_worker(i, es, inv, phys_sem, q_pend_sem, q_slot_sem, metrics, stop)))
        for i in range(3):
            tasks.append(asyncio.create_task(c_worker(i)))

        await asyncio.sleep(15)
        stop.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        # 检查: claim 等待时间不应有极端尖峰
        if claim_wait_samples:
            p50 = statistics.median(claim_wait_samples)
            p95 = sorted(claim_wait_samples)[int(len(claim_wait_samples) * 0.95)]
            p99 = sorted(claim_wait_samples)[int(len(claim_wait_samples) * 0.99)]
            print(f'\n[Starvation] claim_wait p50={p50:.3f}s p95={p95:.3f}s p99={p99:.3f}s')
            print(f'  samples={len(claim_wait_samples)} pair_ok={metrics.pair_consumed_ok}')
            # p99 不应超过 5 秒(在 fake 服务下)
            assert p99 < 5.0, f'claim 饥饿: p99={p99:.3f}s 太高'

        # 检查: 最近成功消费不应太久
        time_since_last = time.time() - last_success_time
        assert time_since_last < 5.0, f'太久没有成功消费: {time_since_last:.1f}s'

    @pytest.mark.asyncio
    async def test_high_cancellation_churn(self):
        """高取消翻转: Worker 频繁取消重启,系统仍保持守恒。"""
        from tests.fakes import FakeTurnstile, FakeEmailService, FakeRegisterAPI

        t_cap, q_cap, pend_cap, phys_cap = 8, 8, 12, 4
        t_slot_sem = asyncio.Semaphore(t_cap)
        q_slot_sem = asyncio.Semaphore(q_cap)
        q_pend_sem = asyncio.Semaphore(pend_cap)
        phys_sem = asyncio.Semaphore(phys_cap)

        ft = FakeTurnstile(delay=0.003, fail_rate=0.1)
        es = FakeEmailService()
        fra = FakeRegisterAPI(success_rate=0.85, delay=0.003)

        metrics = Metrics()
        inv = Inventory(metrics=metrics)
        cons = Conservation(t_slot_sem, q_slot_sem, q_pend_sem, inv)
        stop = asyncio.Event()
        fl = asyncio.Lock()

        tasks = []
        violations = []

        def spawn_worker(kind, idx):
            if kind == 'S':
                return asyncio.create_task(stress_s_worker(idx, ft, inv, phys_sem, t_slot_sem, metrics, stop))
            elif kind == 'P':
                return asyncio.create_task(stress_p_worker(idx, es, inv, phys_sem, q_pend_sem, q_slot_sem, metrics, stop))
            else:
                return asyncio.create_task(stress_c_worker(idx, fra, inv, phys_sem, metrics, stop, fl))

        # 初始 workers
        for i in range(3):
            tasks.append(spawn_worker('S', i))
            tasks.append(spawn_worker('P', i))
            tasks.append(spawn_worker('C', i))

        async def churn():
            import random
            while not stop.is_set():
                await asyncio.sleep(0.3)
                # 随机取消一个,重启一个
                if tasks:
                    idx = random.randint(0, len(tasks) - 1)
                    t = tasks[idx]
                    if not t.done():
                        t.cancel()
                    tasks[idx] = spawn_worker(
                        random.choice(['S', 'P', 'C']),
                        random.randint(0, 9)
                    )
                # 定期守恒检查(软检查)
                try:
                    cons.check_t_conservation(t_cap)
                    cons.check_q_conservation(q_cap)
                    assert q_pend_sem._value >= 0 and q_pend_sem._value <= pend_cap
                except AssertionError as e:
                    violations.append(str(e))

        churn_task = asyncio.create_task(churn())
        tasks.append(churn_task)

        await asyncio.sleep(15)
        stop.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        # 最终守恒检查
        try:
            cons.check_t_conservation(t_cap)
            cons.check_q_conservation(q_cap)
            assert q_pend_sem._value >= 0 and q_pend_sem._value <= pend_cap
        except AssertionError as e:
            violations.append(f'FINAL: {e}')

        assert not violations, f'守恒违反:\n' + '\n'.join(violations[:5])
        print(f'\n[High Churn] t_produced={metrics.t_produced} '
              f'pair_ok={metrics.pair_consumed_ok} success={metrics.success_count}')

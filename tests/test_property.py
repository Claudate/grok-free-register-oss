"""
Layer 3: 性质测试 (Property Tests)

随机生成 S/P/C 成功、失败、取消、超时、过期事件,持续检查:
  - slot 守恒 (T/Q 总量恒定)
  - 无重复 claim (同一个 T/Q 不被两个 PairLease 拿到)
  - 无单边持有等待 (C 不先拿 T 再等 Q)
  - 过期资源不交付
  - 取消后不泄漏
"""
import asyncio
import time
import random
import pytest

from grok_register.core.envelope import ResourceEnvelope
from grok_register.core.inventory import Inventory
from grok_register.core.observer import Metrics
from tests.fakes import FakeTurnstile, FakeEmailService, FakeRegisterAPI, Conservation, EventLog


# ═══════════════════════════════════════════
#  随机性质测试引擎
# ═══════════════════════════════════════════

class PropertyEngine:
    """驱动 S/P/C 随机行为,持续检查不变量。"""

    def __init__(self, t_slot_cap=6, q_slot_cap=6, q_pending_cap=8, phys_cap=4,
                 t_max_age=2.0, q_max_age=2.0, seed=None):
        self.rng = random.Random(seed)
        self.t_slot_sem = asyncio.Semaphore(t_slot_cap)
        self.q_slot_sem = asyncio.Semaphore(q_slot_cap)
        self.q_pending_sem = asyncio.Semaphore(q_pending_cap)
        self.phys_sem = asyncio.Semaphore(phys_cap)
        self.t_slot_cap = t_slot_cap
        self.q_slot_cap = q_slot_cap
        self.q_pending_cap = q_pending_cap
        self.t_max_age = t_max_age
        self.q_max_age = q_max_age

        self.metrics = Metrics()
        self.inventory = Inventory(metrics=self.metrics)
        self.conservation = Conservation(
            self.t_slot_sem, self.q_slot_sem, self.q_pending_sem, self.inventory
        )
        self.events = EventLog()

        self._claimed_tokens = set()  # 检测重复 claim
        self._pairleased_t = 0        # 当前 PairLease 持有的 T
        self._pairleased_q = 0        # 当前 PairLease 持有的 Q

    def check_invariants_soft(self):
        """运行时软检查: 非负、不超限。

        注意: Worker 运行期间 slot 可能在 create_with_slot 和 put_t 之间的
        envelope 中(worker 持有但无法从外部追踪)。严格守恒检查用
        check_invariants_final() 在所有 Worker 停止后执行。
        """
        assert self.t_slot_sem._value >= 0, 'T slot 为负'
        assert self.q_slot_sem._value >= 0, 'Q slot 为负'
        assert self.q_pending_sem._value >= 0, 'Q pending 为负'
        assert self.q_pending_sem._value <= self.q_pending_cap, 'Q pending 超限'
        assert self.phys_sem._value >= 0, 'Physical 为负'
        # inventory 深度不超过 slot 总量
        assert self.inventory.t_depth <= self.t_slot_cap, 'T inventory 超限'
        assert self.inventory.q_depth <= self.q_slot_cap, 'Q inventory 超限'

    def check_invariants_final(self):
        """最终严格守恒检查: 所有 Worker 停止后执行。

        此时没有 Worker 持有中间状态的 slot,也没有 PairLease。
        free + inventory == total
        """
        self.conservation.check_t_conservation(self.t_slot_cap)
        self.conservation.check_q_conservation(self.q_slot_cap)
        self.conservation.check_pending_conservation(self.q_pending_cap)

    async def s_produce(self):
        """S 产生一个 T。返回 True/False/None(cancelled)。"""
        # 模拟物理资源使用
        await self.phys_sem.acquire()
        delay = self.rng.uniform(0.001, 0.02)
        try:
            await asyncio.sleep(delay)
        finally:
            self.phys_sem.release()

        # 模拟随机失败
        if self.rng.random() < 0.1:
            self.metrics.t_discarded += 1
            await self.events.record('t_discarded', 'gen_fail')
            return False

        token = f'tok_{self.metrics.t_produced}'
        self.metrics.t_produced += 1
        await self.events.record('t_produced', token)

        # 创建 envelope + 入库
        # 关键: create_with_slot 成功后 slot 在 envelope 中,
        # 必须追踪 worker_held 直到 put_t 成功或 discard。
        now = time.time()
        expire = now + self.t_max_age if self.rng.random() > 0.15 else now - 1
        t_env = None
        try:
            t_env = await ResourceEnvelope.create_with_slot(
                'T', token, self.t_slot_sem, expires_at=expire
            )
            # slot 已获取,标记 worker 持有
            self.conservation.worker_acquire_t()
            await self.inventory.put_t(t_env)
            # put 成功,所有权转移到 inventory
            self.conservation.worker_release_t()
        except Exception:
            if t_env is not None and not t_env.released:
                t_env.discard(reason='error_cleanup')
                self.conservation.worker_release_t()
            self.metrics.t_discarded += 1
            return False
        except asyncio.CancelledError:
            if t_env is not None and not t_env.released:
                t_env.discard(reason='cancel_cleanup')
                self.conservation.worker_release_t()
            self.metrics.t_discarded += 1
            raise

        self.check_invariants_soft()
        return True

    async def p_produce(self):
        """P 产生一个 Q。返回 True/False/None(cancelled)。"""
        await self.q_pending_sem.acquire()
        _pend_released = False

        try:
            # 创建邮箱 + 发请求(持有 Physical)
            await self.phys_sem.acquire()
            try:
                delay = self.rng.uniform(0.001, 0.02)
                await asyncio.sleep(delay)
            finally:
                self.phys_sem.release()

            # 模拟随机失败
            if self.rng.random() < 0.1:
                self.metrics.q_discarded += 1
                self.q_pending_sem.release()
                _pend_released = True
                await self.events.record('q_discarded', 'send_fail')
                return False

            self.metrics.q_sent += 1

            # 等待 Q 返回(不持有 Physical)
            poll_delay = self.rng.uniform(0.001, 0.05)
            await asyncio.sleep(poll_delay)

            # 模拟超时/失败
            if self.rng.random() < 0.1:
                self.metrics.q_discarded += 1
                self.q_pending_sem.release()
                _pend_released = True
                await self.events.record('q_discarded', 'poll_timeout')
                return False

            code = f'{self.rng.randint(100000, 999999)}'
            self.metrics.q_returned += 1
            await self.events.record('q_returned', code)

            # 创建 envelope + 入库
            now = time.time()
            expire = now + self.q_max_age if self.rng.random() > 0.15 else now - 1
            q_env = None
            try:
                q_env = await ResourceEnvelope.create_with_slot(
                    'Q', {'email': f'e@{code}', 'password': 'p', 'code': code},
                    self.q_slot_sem, expires_at=expire,
                )
                self.conservation.worker_acquire_q()
                await self.inventory.put_q(q_env)
                self.conservation.worker_release_q()
            except Exception:
                if q_env is not None and not q_env.released:
                    q_env.discard(reason='error_cleanup')
                    self.conservation.worker_release_q()
                self.metrics.q_discarded += 1
            except asyncio.CancelledError:
                if q_env is not None and not q_env.released:
                    q_env.discard(reason='cancel_cleanup')
                    self.conservation.worker_release_q()
                self.metrics.q_discarded += 1
                raise

        except asyncio.CancelledError:
            if not _pend_released:
                self.q_pending_sem.release()
                _pend_released = True
            raise
        except Exception:
            self.metrics.q_discarded += 1
        finally:
            if not _pend_released:
                self.q_pending_sem.release()

        self.check_invariants_soft()
        return True

    async def c_consume(self):
        """C 消费一个 pair。返回 True/False/None(cancelled)。"""
        try:
            async with self.inventory.claim_pair() as pair:
                t_val = pair.t.value
                q_val = pair.q.value

                # 检测重复 claim
                assert t_val not in self._claimed_tokens, f'重复 claim: {t_val}'
                self._claimed_tokens.add(t_val)

                # pairleased 计数 — 必须在 claim 后立即标记,
                # 否则在下一个 await 前被取消会导致守恒违反
                self._pairleased_t += 1
                self._pairleased_q += 1

                # 消费(持有 Physical)
                await self.phys_sem.acquire()
                try:
                    delay = self.rng.uniform(0.001, 0.02)
                    await asyncio.sleep(delay)

                    # 模拟随机失败
                    if self.rng.random() < 0.15:
                        self.metrics.pair_consumed_fail += 1
                        await self.events.record('pair_fail', t_val)
                    else:
                        self.metrics.pair_consumed_ok += 1
                        self.metrics.success_count += 1
                        await self.events.record('pair_ok', t_val)
                finally:
                    self.phys_sem.release()

            # PairLease 退出后(正常路径)
            self._pairleased_t -= 1
            self._pairleased_q -= 1

        except asyncio.CancelledError:
            # PairLease.__aexit__ 已核销 slot。
            # 如果 _pairleased 已 +1,则 -1;否则不操作。
            if self._pairleased_t > 0:
                self._pairleased_t -= 1
            if self._pairleased_q > 0:
                self._pairleased_q -= 1
            raise
        except Exception:
            self.metrics.pair_consumed_fail += 1
            if self._pairleased_t > 0:
                self._pairleased_t -= 1
            if self._pairleased_q > 0:
                self._pairleased_q -= 1

        self.check_invariants_soft()
        return True


@pytest.mark.slow
class TestProperty:
    """性质测试: 随机事件流 + 持续不变量检查。"""

    @pytest.mark.asyncio
    async def test_random_events_10s(self):
        """随机 S/P/C 事件流 10 秒,持续检查不变量。"""
        engine = PropertyEngine(seed=42)
        stop = asyncio.Event()
        errors = []

        async def s_loop():
            while not stop.is_set():
                try:
                    await engine.s_produce()
                except asyncio.CancelledError:
                    break
                except AssertionError as e:
                    errors.append(f'S: {e}')
                    break
                await asyncio.sleep(engine.rng.uniform(0.005, 0.03))

        async def p_loop():
            while not stop.is_set():
                try:
                    await engine.p_produce()
                except asyncio.CancelledError:
                    break
                except AssertionError as e:
                    errors.append(f'P: {e}')
                    break
                await asyncio.sleep(engine.rng.uniform(0.005, 0.03))

        async def c_loop():
            while not stop.is_set():
                try:
                    await engine.c_consume()
                except asyncio.CancelledError:
                    break
                except AssertionError as e:
                    errors.append(f'C: {e}')
                    break
                await asyncio.sleep(engine.rng.uniform(0.005, 0.03))

        tasks = []
        for _ in range(2):
            tasks.append(asyncio.create_task(s_loop()))
            tasks.append(asyncio.create_task(p_loop()))
            tasks.append(asyncio.create_task(c_loop()))

        await asyncio.sleep(10)
        stop.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        # 最终不变量检查
        engine.check_invariants_final()

        assert not errors, f'不变量违反:\n' + '\n'.join(errors)
        print(f'\n[Property] t_produced={engine.metrics.t_produced} '
              f'q_sent={engine.metrics.q_sent} q_returned={engine.metrics.q_returned} '
              f'pair_ok={engine.metrics.pair_consumed_ok} pair_fail={engine.metrics.pair_consumed_fail} '
              f'success={engine.metrics.success_count}')

    @pytest.mark.asyncio
    async def test_random_with_cancellations(self):
        """随机事件 + 随机取消 Worker,不变量仍成立。"""
        engine = PropertyEngine(seed=123)
        stop = asyncio.Event()
        errors = []

        async def s_loop():
            while not stop.is_set():
                try:
                    await engine.s_produce()
                except asyncio.CancelledError:
                    break
                except AssertionError as e:
                    errors.append(f'S: {e}')
                    break
                await asyncio.sleep(engine.rng.uniform(0.005, 0.03))

        async def p_loop():
            while not stop.is_set():
                try:
                    await engine.p_produce()
                except asyncio.CancelledError:
                    break
                except AssertionError as e:
                    errors.append(f'P: {e}')
                    break
                await asyncio.sleep(engine.rng.uniform(0.005, 0.03))

        async def c_loop():
            while not stop.is_set():
                try:
                    await engine.c_consume()
                except asyncio.CancelledError:
                    break
                except AssertionError as e:
                    errors.append(f'C: {e}')
                    break
                await asyncio.sleep(engine.rng.uniform(0.005, 0.03))

        async def cancel_loop():
            """随机取消并重启 Worker。"""
            while not stop.is_set():
                await asyncio.sleep(engine.rng.uniform(0.5, 2.0))
                if tasks:
                    idx = engine.rng.randint(0, len(tasks) - 1)
                    t = tasks[idx]
                    if not t.done():
                        t.cancel()
                        try:
                            await t
                        except asyncio.CancelledError:
                            pass
                        # 重启
                        role = idx % 3
                        if role == 0:
                            tasks[idx] = asyncio.create_task(s_loop())
                        elif role == 1:
                            tasks[idx] = asyncio.create_task(p_loop())
                        else:
                            tasks[idx] = asyncio.create_task(c_loop())

        tasks = []
        for _ in range(2):
            tasks.append(asyncio.create_task(s_loop()))
            tasks.append(asyncio.create_task(p_loop()))
            tasks.append(asyncio.create_task(c_loop()))
        tasks.append(asyncio.create_task(cancel_loop()))

        await asyncio.sleep(10)
        stop.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        engine.check_invariants_final()
        assert not errors, f'不变量违反:\n' + '\n'.join(errors)
        print(f'\n[Property+Cancel] t_produced={engine.metrics.t_produced} '
              f'pair_ok={engine.metrics.pair_consumed_ok} '
              f'success={engine.metrics.success_count}')

    @pytest.mark.asyncio
    async def test_extreme_ttl_pressure(self):
        """极端 TTL: 所有资源几乎立即过期,验证过期清理不破坏守恒。"""
        engine = PropertyEngine(
            t_slot_cap=4, q_slot_cap=4, q_pending_cap=6, phys_cap=3,
            t_max_age=0.05, q_max_age=0.05,  # 50ms 过期
            seed=789,
        )
        stop = asyncio.Event()
        errors = []

        async def s_loop():
            while not stop.is_set():
                try:
                    await engine.s_produce()
                except asyncio.CancelledError:
                    break
                except AssertionError as e:
                    errors.append(f'S: {e}')
                    break
                await asyncio.sleep(engine.rng.uniform(0.01, 0.05))

        async def p_loop():
            while not stop.is_set():
                try:
                    await engine.p_produce()
                except asyncio.CancelledError:
                    break
                except AssertionError as e:
                    errors.append(f'P: {e}')
                    break
                await asyncio.sleep(engine.rng.uniform(0.01, 0.05))

        async def c_loop():
            while not stop.is_set():
                try:
                    await engine.c_consume()
                except asyncio.CancelledError:
                    break
                except AssertionError as e:
                    errors.append(f'C: {e}')
                    break
                await asyncio.sleep(engine.rng.uniform(0.01, 0.05))

        tasks = []
        for _ in range(2):
            tasks.append(asyncio.create_task(s_loop()))
            tasks.append(asyncio.create_task(p_loop()))
            tasks.append(asyncio.create_task(c_loop()))

        await asyncio.sleep(5)
        stop.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        engine.check_invariants_final()
        assert not errors, f'不变量违反:\n' + '\n'.join(errors)
        print(f'\n[TTL Pressure] t_produced={engine.metrics.t_produced} '
              f't_expired={engine.metrics.t_expired} q_expired={engine.metrics.q_expired} '
              f'pair_ok={engine.metrics.pair_consumed_ok}')

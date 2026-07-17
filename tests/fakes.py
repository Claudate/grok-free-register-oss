"""
Fake 服务 + 测试工具

FakeTurnstile    — 可控 T 生成器:可注入延迟、失败、取消点
FakeEmailService — 可控 Q 提供者:可注入延迟、超时、验证码
FakeConsumer     — 可控 C 消费器:可注入延迟、失败
Conservation     — slot 守恒断言器
CancelPoint      — 在指定 await 边界注入取消
"""
import asyncio
import time
import dataclasses
from typing import Optional, Callable, Any


# ═══════════════════════════════════════════
#  Fake T 生成器 (替代 solve_one_turnstile)
# ═══════════════════════════════════════════
class FakeTurnstile:
    """可控 token 生成器。

    config:
      delay:      生成延迟(秒)
      fail_rate:  0.0~1.0,随机失败概率
      token_seq:  预设 token 序列,None 则自动递增
    """

    def __init__(self, delay=0.01, fail_rate=0.0, token_seq=None):
        self.delay = delay
        self._fail_rate = fail_rate
        self._seq = token_seq or []
        self._idx = 0
        self._call_count = 0
        self._fail_count = 0
        self._cancel_at: Optional[int] = None  # 第 N 次调用时注入取消

    def set_cancel_at(self, n: int):
        """第 n 次调用时注入 CancelledError。"""
        self._cancel_at = n

    async def generate(self) -> Optional[str]:
        """生成一个 token。返回 None 表示失败,raise CancelledError 表示取消。"""
        self._call_count += 1

        if self._cancel_at is not None and self._call_count == self._cancel_at:
            raise asyncio.CancelledError(f'FakeTurnstile cancel at call {self._call_count}')

        await asyncio.sleep(self.delay)

        if self._fail_count < self._fail_rate * self._call_count:
            return None
        # 简单随机: fail_rate 作为概率
        import random
        if random.random() < self._fail_rate:
            self._fail_count += 1
            return None

        if self._idx < len(self._seq):
            tok = self._seq[self._idx]
            self._idx += 1
            return tok
        self._idx += 1
        return f'fake_token_{self._idx}'

    @property
    def call_count(self):
        return self._call_count


# ═══════════════════════════════════════════
#  Fake 邮箱服务 (替代 create_email + poll_code)
# ═══════════════════════════════════════════
@dataclasses.dataclass
class FakeEmailResult:
    handle: str
    email: str
    password: str
    code: Optional[str] = None    # None = 轮询超时
    create_delay: float = 0.01
    poll_delay: float = 0.01


class FakeEmailService:
    """可控邮箱 + 验证码服务。

    results: 预设结果队列,每次 create+poll 消费一个。
    """

    def __init__(self, results=None):
        self._results = list(results or [])
        self._idx = 0
        self._create_count = 0
        self._poll_count = 0
        self._cancel_create_at: Optional[int] = None
        self._cancel_poll_at: Optional[int] = None

    def set_cancel_create_at(self, n: int):
        self._cancel_create_at = n

    def set_cancel_poll_at(self, n: int):
        self._cancel_poll_at = n

    async def create_email(self):
        """创建邮箱。返回 (handle, email, password)。"""
        self._create_count += 1
        if self._cancel_create_at and self._create_count == self._cancel_create_at:
            raise asyncio.CancelledError(f'FakeEmail.create cancel at {self._create_count}')

        if self._idx < len(self._results):
            r = self._results[self._idx]
            await asyncio.sleep(r.create_delay)
            return r.handle, r.email, r.password
        self._idx += 1
        n = self._idx
        await asyncio.sleep(0.01)
        return f'handle_{n}', f'user_{n}@test.com', f'pass_{n}'

    async def poll_code(self, handle: str, max_wait=90):
        """轮询验证码。返回 code 或 None。"""
        self._poll_count += 1
        if self._cancel_poll_at and self._poll_count == self._cancel_poll_at:
            raise asyncio.CancelledError(f'FakeEmail.poll cancel at {self._poll_count}')

        # 找匹配的预设结果
        for r in self._results:
            if r.handle == handle:
                await asyncio.sleep(r.poll_delay)
                return r.code
        await asyncio.sleep(0.01)
        return '000000'

    @property
    def create_count(self):
        return self._create_count

    @property
    def poll_count(self):
        return self._poll_count


# ═══════════════════════════════════════════
#  Fake 注册 API (替代 grpc_verify + server_action_register)
# ═══════════════════════════════════════════
class FakeRegisterAPI:
    """可控注册 API。"""

    def __init__(self, success_rate=1.0, delay=0.01):
        self.success_rate = success_rate
        self.delay = delay
        self._register_count = 0
        self._cancel_at: Optional[int] = None
        self._fail_at: Optional[int] = None

    def set_cancel_at(self, n: int):
        self._cancel_at = n

    def set_fail_at(self, n: int):
        self._fail_at = n

    async def register(self, email, password, code, token) -> Optional[str]:
        """执行注册。返回 SSO 或 None。"""
        self._register_count += 1
        if self._cancel_at and self._register_count == self._cancel_at:
            raise asyncio.CancelledError(f'FakeRegister cancel at {self._register_count}')
        if self._fail_at and self._register_count == self._fail_at:
            return None
        await asyncio.sleep(self.delay)
        import random
        if random.random() < self.success_rate:
            return f'sso_{email}_{int(time.time())}'
        return None

    @property
    def register_count(self):
        return self._register_count


# ═══════════════════════════════════════════
#  Slot 守恒断言器
# ═══════════════════════════════════════════
class Conservation:
    """检查 T/Q slot 守恒不变量。

    守恒公式:
      slot_total = free_slots + inventory_depth + pairleased_held + worker_held
    """

    def __init__(self, t_slot_sem, q_slot_sem, q_pending_sem, inventory):
        self.t_slot_sem = t_slot_sem
        self.q_slot_sem = q_slot_sem
        self.q_pending_sem = q_pending_sem
        self.inventory = inventory
        # 追踪 Worker 持有的未发布资源
        self._worker_held_t = 0
        self._worker_held_q = 0

    def worker_acquire_t(self):
        self._worker_held_t += 1

    def worker_release_t(self):
        self._worker_held_t -= 1

    def worker_acquire_q(self):
        self._worker_held_q += 1

    def worker_release_q(self):
        self._worker_held_q -= 1

    def check_t_conservation(self, t_slot_total: int, pairleased_t: int = 0):
        """检查 T slot 守恒。

        Args:
            t_slot_total: T_Slot_Sem 初始容量
            pairleased_t: 当前 PairLease 持有的 T 数量
        """
        free = self.t_slot_sem._value
        inv = self.inventory.t_depth
        held = self._worker_held_t
        total = free + inv + pairleased_t + held
        assert total == t_slot_total, (
            f'T slot 守恒违反: free({free}) + inv({inv}) + pairleased({pairleased_t}) '
            f'+ worker_held({held}) = {total} != total({t_slot_total})'
        )

    def check_q_conservation(self, q_slot_total: int, pairleased_q: int = 0):
        """检查 Q slot 守恒。"""
        free = self.q_slot_sem._value
        inv = self.inventory.q_depth
        held = self._worker_held_q
        total = free + inv + pairleased_q + held
        assert total == q_slot_total, (
            f'Q slot 守恒违反: free({free}) + inv({inv}) + pairleased({pairleased_q}) '
            f'+ worker_held({held}) = {total} != total({q_slot_total})'
        )

    def check_pending_conservation(self, pending_total: int, in_flight: int = 0):
        """检查 Q pending 守恒。"""
        free = self.q_pending_sem._value
        total = free + in_flight
        assert total == pending_total, (
            f'Q pending 守恒违反: free({free}) + in_flight({in_flight}) = {total} '
            f'!= total({pending_total})'
        )

    def check_all(self, t_total, q_total, pend_total, pairleased_t=0, pairleased_q=0, in_flight=0):
        """一次性检查所有守恒。"""
        self.check_t_conservation(t_total, pairleased_t)
        self.check_q_conservation(q_total, pairleased_q)
        self.check_pending_conservation(pend_total, in_flight)


# ═══════════════════════════════════════════
#  取消注入器
# ═══════════════════════════════════════════
class CancelInjector:
    """在指定 await 边界前后注入取消。"""

    def __init__(self):
        self._points: dict[str, asyncio.Event] = {}
        self._cancel_task: Optional[asyncio.Task] = None

    def register(self, name: str):
        """注册一个取消点。"""
        self._points[name] = asyncio.Event()

    async def wait_at(self, name: str):
        """Worker 在此挂起,等待外部触发取消。"""
        if name in self._points:
            await self._points[name].wait()

    def trigger(self, name: str, task: asyncio.Task):
        """触发指定取消点,取消目标 task。"""
        if name in self._points:
            self._points[name].set()
            task.cancel()

    async def cancel_after(self, delay: float, task: asyncio.Task):
        """延迟后取消目标 task。"""
        await asyncio.sleep(delay)
        task.cancel()


# ═══════════════════════════════════════════
#  事件记录器(用于性质测试)
# ═══════════════════════════════════════════
@dataclasses.dataclass
class Event:
    ts: float
    kind: str      # 't_produced', 't_admitted', 't_claimed', 'q_sent', 'q_returned',
                   # 'q_admitted', 'q_claimed', 'pair_claimed', 'pair_ok', 'pair_fail',
                   # 't_expired', 'q_expired', 't_discarded', 'q_discarded',
                   # 'sem_t_free', 'sem_q_free', 'sem_pend_free', 'inv_t', 'inv_q'
    detail: Any = None


class EventLog:
    """记录所有事件,用于事后分析。"""

    def __init__(self):
        self.events: list[Event] = []
        self._lock = asyncio.Lock()

    async def record(self, kind: str, detail=None):
        async with self._lock:
            self.events.append(Event(ts=time.time(), kind=kind, detail=detail))

    def count(self, kind: str) -> int:
        return sum(1 for e in self.events if e.kind == kind)

    def filter(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]

    def dump(self) -> str:
        lines = []
        for e in self.events:
            lines.append(f'{e.ts:.3f} {e.kind} {e.detail or ""}')
        return '\n'.join(lines)

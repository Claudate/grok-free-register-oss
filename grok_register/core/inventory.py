"""
Inventory — 唯一库存门面 + PairLease

put_t / put_q: 所有权从 Worker 转移到 Inventory
claim_pair(): 等待完整 T/Q pair,通过 PairLease 原子转移所有权
内部单锁 + Condition,不做分片,不做无锁。
"""
import asyncio
import time
from collections import deque

from .envelope import ResourceEnvelope


class PairLease:
    """claim_pair 成功后的 pair 所有权对象。

    用法:
        async with inventory.claim_pair() as pair:
            t_val, q_val = pair.t.value, pair.q.value
            ...
    离开上下文时无论成功/失败/取消,都核销 T/Q 并释放 slot。
    """

    __slots__ = ('t', 'q', '_inventory')

    def __init__(self, t_env, q_env, inventory):
        self.t = t_env
        self.q = q_env
        self._inventory = inventory

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # 第一版策略: 无论结果如何,均核销 T/Q
        self.t.release_slot_once(reason='pair_settle')
        self.q.release_slot_once(reason='pair_settle')
        # 通知可能在等待的 claim_pair
        async with self._inventory._lock:
            self._inventory._cond.notify_all()
        return False


class Inventory:
    """T/Q 库存门面。

    内部维护两个 deque 和一个 Lock+Condition。
    put_t/put_q 在锁内 O(1) 操作;claim_pair 在锁外等待 Condition。
    """

    def __init__(self, metrics=None):
        self._t_buf = deque()
        self._q_buf = deque()
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)
        self._metrics = metrics

    # ── 入库 ──

    async def put_t(self, env: ResourceEnvelope):
        """将 T envelope 入库。调用前 env 属于 Worker,成功后属于 Inventory。"""
        async with self._lock:
            now = time.time()
            if env.is_expired(now):
                env.discard(reason='expired_on_put')
                if self._metrics:
                    self._metrics.t_expired += 1
                return
            self._cleanup_expired(now)
            self._t_buf.append(env)
            if self._metrics:
                self._metrics.t_admitted += 1
            self._cond.notify_all()

    async def put_q(self, env: ResourceEnvelope):
        """将 Q envelope 入库。调用前 env 属于 Worker,成功后属于 Inventory。"""
        async with self._lock:
            now = time.time()
            if env.is_expired(now):
                env.discard(reason='expired_on_put')
                if self._metrics:
                    self._metrics.q_expired += 1
                return
            self._cleanup_expired(now)
            self._q_buf.append(env)
            if self._metrics:
                self._metrics.q_admitted += 1
            self._cond.notify_all()

    # ── Claim ──

    def claim_pair(self):
        """返回 PairLease 的 async context manager。

        等待期间可取消,取消不弹出资源。成功后 T/Q 原子弹出并转移给 PairLease。
        """
        return _PairClaim(self, self._metrics)

    # ── 内部 ──

    def _try_pop_pair(self):
        """在锁内尝试弹出一对未过期的 T/Q。返回 (t_env, q_env) 或 None。"""
        now = time.time()
        # 清理过期
        self._cleanup_expired(now)
        if self._t_buf and self._q_buf:
            t_env = self._t_buf.popleft()
            q_env = self._q_buf.popleft()
            return t_env, q_env
        return None

    def _cleanup_expired(self, now):
        """lazy cleanup: 清理已过期的 T/Q 并释放 slot。锁内调用。"""
        self._cleanup_buf(self._t_buf, now, 't_expired')
        self._cleanup_buf(self._q_buf, now, 'q_expired')

    def _cleanup_buf(self, buf, now, metric_name):
        """扫描完整库存,保留未过期实体的原始 FIFO 顺序。"""
        kept = deque()
        while buf:
            env = buf.popleft()
            if env.is_expired(now):
                env.discard(reason='expired_in_inventory')
                if self._metrics:
                    setattr(self._metrics, metric_name, getattr(self._metrics, metric_name) + 1)
            else:
                kept.append(env)
        buf.extend(kept)

    @property
    def t_depth(self):
        return len(self._t_buf)

    @property
    def q_depth(self):
        return len(self._q_buf)


class _PairClaim:
    """claim_pair() 返回的 async context manager。"""

    __slots__ = ('_inventory', '_pair', '_metrics')

    def __init__(self, inventory, metrics):
        self._inventory = inventory
        self._pair = None
        self._metrics = metrics

    async def __aenter__(self):
        inv = self._inventory
        async with inv._lock:
            # 循环等待直到有 pair 或被取消
            while True:
                pair = inv._try_pop_pair()
                if pair is not None:
                    t_env, q_env = pair
                    self._pair = PairLease(t_env, q_env, inv)
                    if self._metrics:
                        self._metrics.pair_claimed += 1
                    return self._pair
                # 没有 pair,等待 notify
                await inv._cond.wait()

    async def __aexit__(self, exc_type, exc, tb):
        if self._pair is not None:
            # PairLease.__aexit__ 处理核销
            return await self._pair.__aexit__(exc_type, exc, tb)
        return False

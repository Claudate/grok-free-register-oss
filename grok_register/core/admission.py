"""Local admission gates for CSP workers.

AdmissionGate is not a scheduler: it never chooses a role and never moves
resources. It only lets producers reserve local production intent against
inventory watermarks, so fixed S/P workers can self-throttle without a central
dispatcher.
"""
import asyncio


class _TProductionLease:
    __slots__ = ("_gate", "_released")

    def __init__(self, gate):
        self._gate = gate
        self._released = False

    async def release(self):
        if self._released:
            return
        self._released = True
        async with self._gate._cond:
            self._gate.t_in_progress -= 1
            self._gate._cond.notify_all()


class _QBatchLease:
    __slots__ = ("_gate", "count", "_remaining")

    def __init__(self, gate, count):
        self._gate = gate
        self.count = count
        self._remaining = count

    async def release_one(self):
        if self._remaining <= 0:
            return
        async with self._gate._cond:
            if self._remaining <= 0:
                return
            self._remaining -= 1
            self._gate.q_inflight -= 1
            self._gate._cond.notify_all()

    async def release_all(self):
        if self._remaining <= 0:
            return
        async with self._gate._cond:
            if self._remaining <= 0:
                return
            self._gate.q_inflight -= self._remaining
            self._remaining = 0
            self._gate._cond.notify_all()


class AdmissionGate:
    """Watermark-based producer admission for T and Q.

    T production uses high/low hysteresis because local T generation can be
    heavy and should not keep filling an already sufficient inventory.

    Q production reserves a batch by current demand:
        q_high - q_depth - q_inflight
    capped by the worker's max batch. Each reservation is released when the
    corresponding pending Q requests have reached terminal state.
    """

    def __init__(self, inventory, *, t_low, t_high, q_low, q_high):
        if t_low < 0 or q_low < 0:
            raise ValueError("low watermarks must be non-negative")
        if t_high <= 0 or q_high <= 0:
            raise ValueError("high watermarks must be positive")
        if t_low > t_high:
            raise ValueError("t_low must be <= t_high")
        if q_low > q_high:
            raise ValueError("q_low must be <= q_high")

        self.inventory = inventory
        self.t_low = t_low
        self.t_high = t_high
        self.q_low = q_low
        self.q_high = q_high
        self.t_in_progress = 0
        self.q_inflight = 0
        self._t_paused = False
        self._q_paused = False
        self._cond = asyncio.Condition()

    async def acquire_t_production(self):
        """Reserve one T production attempt."""
        async with self._cond:
            while True:
                total = self.inventory.t_depth + self.t_in_progress
                if total >= self.t_high:
                    self._t_paused = True
                if self._t_paused and total <= self.t_low:
                    self._t_paused = False
                if not self._t_paused and total < self.t_high:
                    self.t_in_progress += 1
                    self._cond.notify_all()
                    return _TProductionLease(self)
                await self._cond.wait()

    async def acquire_q_batch(self, *, max_batch):
        """Reserve up to max_batch pending Q requests based on current demand."""
        if max_batch <= 0:
            raise ValueError("max_batch must be positive")

        async with self._cond:
            while True:
                total = self.inventory.q_depth + self.q_inflight
                if total >= self.q_high:
                    self._q_paused = True
                if self._q_paused and total <= self.q_low:
                    self._q_paused = False
                demand = self.q_high - total
                if not self._q_paused and demand > 0:
                    count = min(max_batch, demand)
                    self.q_inflight += count
                    if self.inventory.q_depth + self.q_inflight >= self.q_high:
                        self._q_paused = True
                    self._cond.notify_all()
                    return _QBatchLease(self, count)
                await self._cond.wait()

    async def notify_changed(self):
        """Wake waiters after inventory depth changes."""
        async with self._cond:
            self._cond.notify_all()

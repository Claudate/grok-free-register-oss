"""
Observer — 只读观测层

只记录指标、生成日志。不修改 Semaphore,不调度 Worker,不释放资源。
"""
import time
from collections import deque


class Metrics:
    """全局指标收集器。所有字段只在 asyncio 单线程中写入,无需加锁。"""

    __slots__ = (
        'start_time',
        't_produced', 't_admitted', 't_claimed', 't_expired', 't_discarded',
        't_solve_count', 't_solve_seconds', 't_solve_failed',
        'solver_goto_seconds', 'solver_inject_seconds', 'solver_initial_seconds',
        'solver_click_seconds', 'solver_wait_seconds', 'solver_reused_count',
        'solver_visible_frame_count',
        's_physical_count', 's_physical_wait_seconds', 's_physical_hold_seconds',
        'p_physical_count', 'p_physical_wait_seconds', 'p_physical_hold_seconds',
        'c_physical_count', 'c_physical_wait_seconds', 'c_physical_hold_seconds',
        'p_email_create_count', 'p_email_create_seconds',
        'p_page_prepare_count', 'p_page_prepare_seconds',
        'p_send_count', 'p_send_seconds',
        'c_page_acquire_count', 'c_page_acquire_seconds',
        'c_verify_count', 'c_verify_seconds',
        'c_register_count', 'c_register_seconds',
        'c_hot_page_hits', 'c_hot_page_misses',
        'q_sent', 'q_returned', 'q_admitted', 'q_claimed', 'q_expired', 'q_discarded',
        'q_send_batches', 'q_send_batch_items',
        'pair_claimed', 'pair_consumed_ok', 'pair_consumed_fail',
        'success_count',
        'registration_starts',
        '_clock', 'started_monotonic', 'recent_success_times',
    )

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.started_monotonic = clock()
        self.recent_success_times = deque()
        self.start_time = time.time()
        # T 生命周期
        self.t_produced = 0
        self.t_admitted = 0
        self.t_claimed = 0
        self.t_expired = 0
        self.t_discarded = 0
        self.t_solve_count = 0
        self.t_solve_seconds = 0.0
        self.t_solve_failed = 0
        self.solver_goto_seconds = 0.0
        self.solver_inject_seconds = 0.0
        self.solver_initial_seconds = 0.0
        self.solver_click_seconds = 0.0
        self.solver_wait_seconds = 0.0
        self.solver_reused_count = 0
        self.solver_visible_frame_count = 0
        self.s_physical_count = 0
        self.s_physical_wait_seconds = 0.0
        self.s_physical_hold_seconds = 0.0
        self.p_physical_count = 0
        self.p_physical_wait_seconds = 0.0
        self.p_physical_hold_seconds = 0.0
        self.c_physical_count = 0
        self.c_physical_wait_seconds = 0.0
        self.c_physical_hold_seconds = 0.0
        self.p_email_create_count = 0
        self.p_email_create_seconds = 0.0
        self.p_page_prepare_count = 0
        self.p_page_prepare_seconds = 0.0
        self.p_send_count = 0
        self.p_send_seconds = 0.0
        self.c_page_acquire_count = 0
        self.c_page_acquire_seconds = 0.0
        self.c_verify_count = 0
        self.c_verify_seconds = 0.0
        self.c_register_count = 0
        self.c_register_seconds = 0.0
        self.c_hot_page_hits = 0
        self.c_hot_page_misses = 0
        # Q 生命周期
        self.q_sent = 0
        self.q_returned = 0
        self.q_admitted = 0
        self.q_claimed = 0
        self.q_expired = 0
        self.q_discarded = 0
        self.q_send_batches = 0
        self.q_send_batch_items = 0
        # Pair
        self.pair_claimed = 0
        self.pair_consumed_ok = 0
        self.pair_consumed_fail = 0
        # 成功数
        self.success_count = 0
        self.registration_starts = 0

    def next_registration_task(self):
        self.registration_starts += 1
        return self.registration_starts

    def record_success(self):
        self.success_count += 1
        self.recent_success_times.append(self._clock())

    def five_minute_success_rate(self):
        now = self._clock()
        cutoff = now - 300.0
        while self.recent_success_times and self.recent_success_times[0] < cutoff:
            self.recent_success_times.popleft()
        if not self.recent_success_times:
            return None if self.success_count == 0 else 0.0
        elapsed = max(1.0, min(300.0, now - self.started_monotonic))
        return len(self.recent_success_times) * 60.0 / elapsed

    def runtime_average_success_rate(self):
        if self.success_count == 0:
            return None
        elapsed = max(1.0, self._clock() - self.started_monotonic)
        return self.success_count * 60.0 / elapsed

    def snapshot(self, inventory, sems):
        """生成一行监控日志。"""
        elapsed = time.time() - self.start_time
        rate = self.success_count / (elapsed / 60) if elapsed > 60 else 0
        p_batch_avg = (
            self.q_send_batch_items / self.q_send_batches
            if self.q_send_batches else 0
        )
        t_solve_avg = (
            self.t_solve_seconds / self.t_solve_count
            if self.t_solve_count else 0
        )
        solver_goto_avg = (
            self.solver_goto_seconds / self.t_solve_count
            if self.t_solve_count else 0
        )
        solver_inject_avg = (
            self.solver_inject_seconds / self.t_solve_count
            if self.t_solve_count else 0
        )
        solver_initial_avg = (
            self.solver_initial_seconds / self.t_solve_count
            if self.t_solve_count else 0
        )
        solver_click_avg = (
            self.solver_click_seconds / self.t_solve_count
            if self.t_solve_count else 0
        )
        solver_wait_avg = (
            self.solver_wait_seconds / self.t_solve_count
            if self.t_solve_count else 0
        )
        solver_reuse_ratio = (
            self.solver_reused_count / self.t_solve_count
            if self.t_solve_count else 0
        )
        solver_visible_ratio = (
            self.solver_visible_frame_count / self.t_solve_count
            if self.t_solve_count else 0
        )
        s_phys_wait, s_phys_hold = self._avg_pair(
            self.s_physical_wait_seconds, self.s_physical_hold_seconds, self.s_physical_count
        )
        p_phys_wait, p_phys_hold = self._avg_pair(
            self.p_physical_wait_seconds, self.p_physical_hold_seconds, self.p_physical_count
        )
        c_phys_wait, c_phys_hold = self._avg_pair(
            self.c_physical_wait_seconds, self.c_physical_hold_seconds, self.c_physical_count
        )
        p_email_create = self._avg(self.p_email_create_seconds, self.p_email_create_count)
        p_page_prepare = self._avg(self.p_page_prepare_seconds, self.p_page_prepare_count)
        p_send_stage = self._avg(self.p_send_seconds, self.p_send_count)
        c_page_acquire = self._avg(self.c_page_acquire_seconds, self.c_page_acquire_count)
        c_verify = self._avg(self.c_verify_seconds, self.c_verify_count)
        c_register = self._avg(self.c_register_seconds, self.c_register_count)
        p_send_sem = sems.get("p_send")
        admission = sems.get("admission")
        p_send_part = f' p_send:{p_send_sem._value}' if p_send_sem is not None else ''
        admission_part = (
            f' t_prog:{admission.t_in_progress} q_inflight:{admission.q_inflight}'
            if admission is not None else ''
        )
        return (
            f'[*] T:{inventory.t_depth} Q:{inventory.q_depth} '
            f'phys:{sems["physical"]._value}{p_send_part} t_slot:{sems["t_slot"]._value} '
            f'q_slot:{sems["q_slot"]._value} q_pend:{sems["q_pending"]._value} '
            f'p_batch:{p_batch_avg:.1f}{admission_part} '
            f's_phys:{s_phys_wait:.2f}/{s_phys_hold:.2f} '
            f'p_phys:{p_phys_wait:.2f}/{p_phys_hold:.2f} '
            f'c_phys:{c_phys_wait:.2f}/{c_phys_hold:.2f} '
            f'p_stage:{p_email_create:.2f}/{p_page_prepare:.2f}/{p_send_stage:.2f} '
            f'c_stage:{c_page_acquire:.2f}/{c_verify:.2f}/{c_register:.2f} '
            f'c_hot:{self.c_hot_page_hits}/{self.c_hot_page_misses} '
            f't_solve_avg:{t_solve_avg:.1f} t_solve_fail:{self.t_solve_failed} '
            f'solver_goto:{solver_goto_avg:.2f} solver_inject:{solver_inject_avg:.2f} '
            f'solver_initial:{solver_initial_avg:.2f} solver_click:{solver_click_avg:.2f} '
            f'solver_wait:{solver_wait_avg:.2f} solver_reuse:{solver_reuse_ratio:.2f} '
            f'solver_visible:{solver_visible_ratio:.2f} '
            f't_prod:{self.t_produced} t_adm:{self.t_admitted} t_exp:{self.t_expired} '
            f'q_sent:{self.q_sent} q_ret:{self.q_returned} q_adm:{self.q_admitted} q_exp:{self.q_expired} '
            f'pair:{self.pair_claimed} ok:{self.pair_consumed_ok} fail:{self.pair_consumed_fail} '
            f'rate:{rate:.1f}/min #{self.success_count}'
        )

    @staticmethod
    def _avg(total, count):
        return total / count if count else 0

    @staticmethod
    def _avg_pair(wait_total, hold_total, count):
        if not count:
            return 0, 0
        return wait_total / count, hold_total / count

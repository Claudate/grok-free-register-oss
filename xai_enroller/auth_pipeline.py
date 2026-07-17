"""Bounded asynchronous local authentication pipeline."""

import asyncio
import heapq
import itertools
import time
from collections import deque
from contextlib import suppress
from dataclasses import replace

import httpx

from .models import (
    AuthorizationStatus,
    CompletionJob,
    JobStatus,
    PipelineState,
    PreparedJob,
)


class GlobalRateLimitGate:
    """Process-wide cooldown with exactly one confirmation probe.

    Cooldown grows after consecutive trips so a cleared probe does not immediately
    re-enter a tight authorization loop that re-trips the upstream limit.
    """

    COOLDOWN_SECONDS = 60.0
    MAX_COOLDOWN_SECONDS = 300.0
    COOLDOWN_GROWTH = 1.5

    def __init__(self, *, clock=time.monotonic, base_cooldown=None):
        self._clock = clock
        self._condition = asyncio.Condition()
        self._tripped_at = None
        self._next_probe_at = 0.0
        self._probe_token = None
        self._closed = False
        self.base_cooldown = float(
            self.COOLDOWN_SECONDS if base_cooldown is None else base_cooldown
        )
        # Keep class attribute in sync for callers that still read COOLDOWN_SECONDS.
        self.COOLDOWN_SECONDS = self.base_cooldown
        self._current_cooldown = self.base_cooldown
        self._consecutive_trips = 0

    async def wait_for_permission(self, resume_event):
        while True:
            await resume_event.wait()
            async with self._condition:
                if self._closed:
                    raise asyncio.CancelledError
                if not resume_event.is_set():
                    continue
                if self._tripped_at is None:
                    return None
                now = self._clock()
                if self._probe_token is None and now >= self._next_probe_at:
                    self._probe_token = object()
                    return self._probe_token
                timeout = max(0.0, self._next_probe_at - now)
                try:
                    if timeout:
                        await asyncio.wait_for(self._condition.wait(), timeout=timeout)
                    else:
                        await self._condition.wait()
                except TimeoutError:
                    pass

    async def rate_limited(self, probe_token=None):
        async with self._condition:
            now = self._clock()
            if self._tripped_at is None:
                self._tripped_at = now
                self._consecutive_trips = 1
            elif probe_token is None or probe_token is not self._probe_token:
                return False
            else:
                self._consecutive_trips = max(1, self._consecutive_trips) + 1
            growth = self.COOLDOWN_GROWTH ** max(0, self._consecutive_trips - 1)
            self._current_cooldown = min(
                self.MAX_COOLDOWN_SECONDS,
                self.base_cooldown * growth,
            )
            self.COOLDOWN_SECONDS = self._current_cooldown
            self._next_probe_at = now + self._current_cooldown
            self._probe_token = None
            self._condition.notify_all()
            return True

    async def authorized(self, probe_token=None):
        async with self._condition:
            if self._tripped_at is None:
                return None
            if probe_token is None or probe_token is not self._probe_token:
                return None
            elapsed = max(0.0, self._clock() - self._tripped_at)
            self._tripped_at = None
            self._next_probe_at = 0.0
            self._probe_token = None
            self._consecutive_trips = 0
            self._current_cooldown = self.base_cooldown
            self.COOLDOWN_SECONDS = self.base_cooldown
            self._condition.notify_all()
            return elapsed

    async def inconclusive(self, probe_token=None):
        async with self._condition:
            if (
                self._tripped_at is None
                or probe_token is None
                or probe_token is not self._probe_token
            ):
                return False
            self._next_probe_at = self._clock() + self._current_cooldown
            self._probe_token = None
            self._condition.notify_all()
            return True

    async def close(self):
        async with self._condition:
            self._closed = True
            self._probe_token = None
            self._condition.notify_all()

    def snapshot(self):
        now = self._clock()
        return {
            "cooldown": self._tripped_at is not None,
            "probe_in_flight": self._probe_token is not None,
            "cooldown_remaining_seconds": (
                round(max(0.0, self._next_probe_at - now), 1)
                if self._tripped_at is not None
                else 0.0
            ),
            "cooldown_seconds": round(self._current_cooldown, 1),
            "rate_limit_trips": self._consecutive_trips,
        }


class MinimumStartInterval:
    """Enforce a minimum interval between actual authorization starts.

    The configured base interval is the floor. After an upstream rate limit the
    live interval steps up so the service does not immediately re-flood the
    device-verify endpoint; it decays back toward the base after a streak of
    successful authorizations.
    """

    RATE_LIMIT_FLOOR_SECONDS = 18.0
    RATE_LIMIT_GROWTH = 1.55
    SUCCESS_DECAY = 0.88
    SUCCESS_STREAK_TO_DECAY = 4

    def __init__(
        self,
        seconds=0.0,
        *,
        max_seconds=None,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ):
        self.base_seconds = max(0.0, float(seconds))
        self.seconds = self.base_seconds
        self.max_seconds = float(
            max_seconds
            if max_seconds is not None
            else max(self.base_seconds * 8.0, 90.0)
        )
        self._clock = clock
        self._sleep = sleep
        self._last_started_at = self._clock() if self.seconds > 0 else None
        self._success_streak = 0

    async def wait(self):
        if self._last_started_at is None or self.seconds <= 0:
            return
        delay = self.seconds - (self._clock() - self._last_started_at)
        if delay > 0:
            await self._sleep(delay)

    def mark_started(self):
        self._last_started_at = self._clock()

    def note_rate_limited(self):
        """Raise the live start interval after an upstream rate-limit trip."""
        floor = max(self.base_seconds, self.RATE_LIMIT_FLOOR_SECONDS)
        raised = max(self.seconds * self.RATE_LIMIT_GROWTH, floor)
        self.seconds = min(self.max_seconds, raised)
        self._success_streak = 0
        return self.seconds

    def note_success(self):
        """Decay the live interval toward the configured base after successes."""
        self._success_streak += 1
        if (
            self._success_streak < self.SUCCESS_STREAK_TO_DECAY
            or self.seconds <= self.base_seconds
        ):
            return self.seconds
        self.seconds = max(self.base_seconds, self.seconds * self.SUCCESS_DECAY)
        if self.seconds <= self.base_seconds + 0.05:
            self.seconds = self.base_seconds
        self._success_streak = 0
        return self.seconds

    def snapshot(self):
        if self._last_started_at is None:
            remaining = 0.0
        else:
            remaining = max(
                0.0,
                self.seconds - (self._clock() - self._last_started_at),
            )
        return {
            "min_authorization_interval_seconds": round(self.seconds, 1),
            "base_authorization_interval_seconds": round(self.base_seconds, 1),
            "pacing_remaining_seconds": round(remaining, 1),
        }


class AuthPipeline:
    SOURCE_QUEUE_CAPACITY = 64
    PREPARED_QUEUE_CAPACITY = 1
    COMPLETION_QUEUE_CAPACITY = 2
    FLOW_SAFETY_MARGIN_SECONDS = 60.0
    TRANSIENT_RETRY_DELAYS = (5.0, 15.0, 30.0)
    CANCEL_RETRY_DELAY = 60.0

    def __init__(
        self,
        *,
        source,
        protocol,
        executor,
        sink,
        ledger,
        timeout=1800.0,
        min_authorization_interval=0.0,
        event_callback=None,
        clock=time.monotonic,
    ):
        self.source = source
        self.protocol = protocol
        self.executor = executor
        self.sink = sink
        self.ledger = ledger
        self.timeout = float(timeout)
        self.event_callback = event_callback
        self._clock = clock

        self.source_queue = asyncio.Queue(maxsize=self.SOURCE_QUEUE_CAPACITY)
        self.prepared_queue = asyncio.Queue(maxsize=self.PREPARED_QUEUE_CAPACITY)
        self.completion_queue = asyncio.Queue(maxsize=self.COMPLETION_QUEUE_CAPACITY)
        self.rate_gate = GlobalRateLimitGate(clock=clock)
        self.start_interval = MinimumStartInterval(
            min_authorization_interval,
            clock=clock,
        )

        self._states = {}
        self._transient_retries = {}
        self._retry_heap = []
        self._retry_sequence = itertools.count()
        self._retry_condition = asyncio.Condition()
        self._resume = asyncio.Event()
        self._resume.set()
        self._prepare_slot_available = asyncio.Event()
        self._prepare_slot_available.set()
        self._stop_requested = asyncio.Event()
        self._stopping = False
        self._running = False
        self._tasks = []

        self._authorization_task = None
        self._authorization_cancellable = False
        self._completion_task = None
        self._completion_cancellable = False
        self._started_monotonic = self._clock()
        self._process_imports = 0
        self._authorization_starts = 0
        self._recent_imports = deque()

        self.ledger.recover_pending()
        for fingerprint in self.ledger.imported_fingerprints():
            self._states[fingerprint] = PipelineState.IMPORTED

    def _emit(self, kind, data):
        if self.event_callback is None:
            return
        try:
            self.event_callback(kind, data)
        except Exception:
            pass

    async def _guard(self, stage, coroutine):
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except Exception:
            self._emit("pipeline_error", {"stage": stage, "reason": "internal_error"})
            self.request_stop()

    async def run(self):
        if self._running:
            raise RuntimeError("authentication pipeline is already running")
        self._running = True
        self._started_monotonic = self._clock()
        try:
            if hasattr(self.executor, "start"):
                await self.executor.start()
            self._tasks = [
                asyncio.create_task(self._guard("source", self._source_worker())),
                asyncio.create_task(self._guard("retry", self._retry_worker())),
                asyncio.create_task(self._guard("prepare", self._prepare_worker())),
                asyncio.create_task(
                    self._guard("authorization", self._authorization_worker())
                ),
                asyncio.create_task(self._guard("completion", self._completion_worker())),
            ]
            self._emit("service_started", self.start_interval.snapshot())
            await self._stop_requested.wait()
        finally:
            await self._shutdown()

    def request_stop(self):
        self._stop_requested.set()

    async def _shutdown(self):
        if self._stopping:
            return
        self._stopping = True
        self._resume.set()
        await self.rate_gate.close()
        async with self._retry_condition:
            self._retry_condition.notify_all()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        with suppress(Exception):
            await self.source.close()
        if hasattr(self.executor, "close"):
            with suppress(Exception):
                await self.executor.close()
        self._settle_buffered_attempts()
        self.ledger.recover_pending(reason="shutdown_cancelled")
        self._running = False
        self._emit("service_stopped", {})

    def _settle_buffered_attempts(self):
        for queue in (self.prepared_queue, self.completion_queue):
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                prepared = item.prepared if isinstance(item, CompletionJob) else item
                self.ledger.finish(
                    prepared.job_id, JobStatus.CANCELLED, "shutdown_cancelled"
                )
                queue.task_done()
        while True:
            try:
                self.source_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.source_queue.task_done()
        self._retry_heap.clear()

    def pause(self):
        self._resume.clear()

    def resume(self):
        self._resume.set()

    async def cancel_active(self):
        task = None
        if (
            self._authorization_cancellable
            and self._authorization_task is not None
            and not self._authorization_task.done()
        ):
            task = self._authorization_task
            self._authorization_cancellable = False
        elif (
            self._completion_cancellable
            and self._completion_task is not None
            and not self._completion_task.done()
        ):
            task = self._completion_task
            self._completion_cancellable = False
        if task is None:
            return False
        task.cancel()
        return True

    async def admit(self, source):
        fingerprint = self.ledger.fingerprint(source.source_id)
        if fingerprint in self._states:
            return False
        self._states[fingerprint] = PipelineState.QUEUED
        try:
            await self.source_queue.put(source)
        except BaseException:
            if self._states.get(fingerprint) is PipelineState.QUEUED:
                self._states.pop(fingerprint, None)
            raise
        self._emit("source_admitted", {"queued": self.source_queue.qsize()})
        return True

    async def _source_worker(self):
        async for source in self.source.records():
            if self._stopping:
                return
            await self.admit(source)

    def _set_state(self, fingerprint, state):
        self._states[fingerprint] = state

    async def _schedule_retry(self, source, fingerprint, delay):
        self._set_state(fingerprint, PipelineState.RETRY_WAITING)
        due = self._clock() + delay
        async with self._retry_condition:
            heapq.heappush(
                self._retry_heap,
                (due, next(self._retry_sequence), fingerprint, source),
            )
            self._retry_condition.notify_all()

    async def _retry_worker(self):
        while not self._stopping:
            async with self._retry_condition:
                while not self._retry_heap and not self._stopping:
                    await self._retry_condition.wait()
                if self._stopping:
                    return
                due = self._retry_heap[0][0]
                delay = due - self._clock()
                if delay > 0:
                    try:
                        await asyncio.wait_for(self._retry_condition.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    continue
                _due, _sequence, fingerprint, source = heapq.heappop(self._retry_heap)
            if self._states.get(fingerprint) is not PipelineState.RETRY_WAITING:
                continue
            self._states[fingerprint] = PipelineState.QUEUED
            await self.source_queue.put(source)

    async def _start_flow(self):
        async with asyncio.timeout(self.timeout):
            return await self.protocol.start_device_flow()

    async def _prepare_worker(self):
        while not self._stopping:
            await self._prepare_slot_available.wait()
            self._prepare_slot_available.clear()
            source = await self.source_queue.get()
            fingerprint = self.ledger.fingerprint(source.source_id)
            attempt = self.ledger.next_attempt(fingerprint)
            job_id = self.ledger.start_fingerprint(fingerprint, attempt=attempt)
            try:
                try:
                    flow = await self._start_flow()
                except asyncio.CancelledError:
                    self.ledger.finish(job_id, JobStatus.CANCELLED, "shutdown_cancelled")
                    raise
                except ValueError:
                    await self._finish_failure(
                        source,
                        fingerprint,
                        job_id,
                        attempt,
                        JobStatus.SOURCE_INVALID,
                        "device_flow_invalid",
                        retry=False,
                    )
                    self._prepare_slot_available.set()
                    continue
                except Exception:
                    await self._finish_failure(
                        source,
                        fingerprint,
                        job_id,
                        attempt,
                        JobStatus.TRANSPORT_FAILED,
                        "device_flow_failed",
                        retry=True,
                    )
                    self._prepare_slot_available.set()
                    continue
                prepared = PreparedJob(
                    source=source,
                    source_fingerprint=fingerprint,
                    flow=flow,
                    flow_created_monotonic=self._clock(),
                    job_id=job_id,
                    attempt_number=attempt,
                )
                self._set_state(fingerprint, PipelineState.PREPARED)
                try:
                    await self.prepared_queue.put(prepared)
                except asyncio.CancelledError:
                    self.ledger.finish(job_id, JobStatus.CANCELLED, "shutdown_cancelled")
                    raise
            finally:
                self.source_queue.task_done()

    def _flow_remaining(self, prepared):
        age = max(0.0, self._clock() - prepared.flow_created_monotonic)
        return float(prepared.flow.expires_in) - age

    async def _refresh_flow_if_needed(self, prepared):
        if self._flow_remaining(prepared) >= self.FLOW_SAFETY_MARGIN_SECONDS:
            return prepared
        flow = await self._start_flow()
        refreshed = replace(
            prepared,
            flow=flow,
            flow_created_monotonic=self._clock(),
        )
        if self._flow_remaining(refreshed) < self.FLOW_SAFETY_MARGIN_SECONDS:
            raise RuntimeError("device flow lifetime below safety margin")
        return refreshed

    async def _authorization_worker(self):
        while not self._stopping:
            prepared = await self.prepared_queue.get()
            self._prepare_slot_available.set()
            task = asyncio.create_task(self._authorize(prepared))
            self._authorization_task = task
            try:
                await task
            except asyncio.CancelledError:
                if self._stopping:
                    raise
            finally:
                self._authorization_cancellable = False
                self._authorization_task = None
                self.prepared_queue.task_done()

    async def _finish_if_already_imported(self, prepared, probe_token=None):
        if not self.ledger.has_imported(prepared.source.source_id):
            return False
        self._authorization_cancellable = False
        self.ledger.finish(
            prepared.job_id,
            JobStatus.CANCELLED,
            "already_imported",
        )
        self._set_state(prepared.source_fingerprint, PipelineState.IMPORTED)
        self._transient_retries.pop(prepared.source_fingerprint, None)
        await self.rate_gate.inconclusive(probe_token)
        return True

    async def _authorize(self, prepared):
        probe_token = None
        self._authorization_cancellable = True
        try:
            await self.start_interval.wait()
            await self._resume.wait()
            if await self._finish_if_already_imported(prepared):
                return
            probe_token = await self.rate_gate.wait_for_permission(self._resume)
            while True:
                await self._resume.wait()
                try:
                    prepared = await self._refresh_flow_if_needed(prepared)
                except ValueError:
                    self._authorization_cancellable = False
                    await self.rate_gate.inconclusive(probe_token)
                    await self._finish_failure(
                        prepared.source,
                        prepared.source_fingerprint,
                        prepared.job_id,
                        prepared.attempt_number,
                        JobStatus.SOURCE_INVALID,
                        "device_flow_invalid",
                        retry=False,
                    )
                    return
                except Exception:
                    self._authorization_cancellable = False
                    await self.rate_gate.inconclusive(probe_token)
                    await self._finish_failure(
                        prepared.source,
                        prepared.source_fingerprint,
                        prepared.job_id,
                        prepared.attempt_number,
                        JobStatus.TRANSPORT_FAILED,
                        "device_flow_refresh_failed",
                        retry=True,
                    )
                    return
                if self._resume.is_set() and self._flow_remaining(
                    prepared
                ) >= self.FLOW_SAFETY_MARGIN_SECONDS:
                    break

            if await self._finish_if_already_imported(prepared, probe_token):
                return

            self._set_state(prepared.source_fingerprint, PipelineState.ACTIVE)
            self.start_interval.mark_started()
            self.ledger.mark_authorization_started(prepared.job_id)
            self._authorization_starts += 1
            prepared = replace(prepared, task_number=self._authorization_starts)
            self._emit(
                "authorization_started",
                {
                    "task_number": self._authorization_starts,
                    "attempt_number": prepared.attempt_number,
                    "source_queue": self.source_queue.qsize(),
                    "pending_total": self._pending_total(),
                },
            )
            async with asyncio.timeout(self.timeout):
                authorization = await self.executor.confirm(
                    prepared.source, prepared.flow
                )
            self._authorization_cancellable = False
            if authorization.status is AuthorizationStatus.AUTHORIZED:
                recovery_elapsed = await self.rate_gate.authorized(probe_token)
                if recovery_elapsed is not None:
                    self._emit(
                        "rate_limit_cleared",
                        {"elapsed_seconds": round(recovery_elapsed)},
                    )
                paced = self.start_interval.note_success()
                if paced > self.start_interval.base_seconds:
                    self._emit(
                        "pacing_adjusted",
                        {
                            "min_authorization_interval_seconds": round(paced, 1),
                            "reason": "success_decay",
                        },
                    )
                await self.completion_queue.put(CompletionJob(prepared))
                return

            if authorization.reason_code == "rate_limited":
                applied = await self.rate_gate.rate_limited(probe_token)
                if applied:
                    paced = self.start_interval.note_rate_limited()
                    self._emit(
                        "rate_limited",
                        {
                            "wait_seconds": int(self.rate_gate.COOLDOWN_SECONDS),
                            "min_authorization_interval_seconds": round(paced, 1),
                        },
                    )
                await self._finish_failure(
                    prepared.source,
                    prepared.source_fingerprint,
                    prepared.job_id,
                    prepared.attempt_number,
                    JobStatus.NEEDS_INTERACTION,
                    "rate_limited",
                    retry="rate_limited",
                    task_number=prepared.task_number,
                )
                return

            await self.rate_gate.inconclusive(probe_token)
            transient = authorization.reason_code in {
                "browser_error",
                "confirmation_timeout",
                "challenge_required",
                "unknown_page",
            }
            if authorization.reason_code == "confirmation_timeout":
                status = JobStatus.TIMEOUT
            else:
                try:
                    status = JobStatus(authorization.status.value)
                except ValueError:
                    status = JobStatus.NEEDS_INTERACTION
            await self._finish_failure(
                prepared.source,
                prepared.source_fingerprint,
                prepared.job_id,
                prepared.attempt_number,
                status,
                authorization.reason_code,
                retry=transient,
                task_number=prepared.task_number,
            )
        except asyncio.CancelledError:
            self._authorization_cancellable = False
            await self.rate_gate.inconclusive(probe_token)
            reason = "shutdown_cancelled" if self._stopping else "operator_cancelled"
            self.ledger.finish(prepared.job_id, JobStatus.CANCELLED, reason)
            if not self._stopping:
                await self._schedule_retry(
                    prepared.source,
                    prepared.source_fingerprint,
                    self.CANCEL_RETRY_DELAY,
                )
                self._emit_result(
                    JobStatus.CANCELLED,
                    reason,
                    prepared.attempt_number,
                    prepared.task_number,
                )
            raise
        except TimeoutError:
            self._authorization_cancellable = False
            await self.rate_gate.inconclusive(probe_token)
            await self._finish_failure(
                prepared.source,
                prepared.source_fingerprint,
                prepared.job_id,
                prepared.attempt_number,
                JobStatus.TIMEOUT,
                "confirmation_timeout",
                retry=True,
                task_number=prepared.task_number,
            )
        except (httpx.TransportError, OSError):
            self._authorization_cancellable = False
            await self.rate_gate.inconclusive(probe_token)
            await self._finish_failure(
                prepared.source,
                prepared.source_fingerprint,
                prepared.job_id,
                prepared.attempt_number,
                JobStatus.TRANSPORT_FAILED,
                "authorization_transport_failed",
                retry=True,
                task_number=prepared.task_number,
            )
        except Exception:
            self._authorization_cancellable = False
            await self.rate_gate.inconclusive(probe_token)
            await self._finish_failure(
                prepared.source,
                prepared.source_fingerprint,
                prepared.job_id,
                prepared.attempt_number,
                JobStatus.NEEDS_INTERACTION,
                "browser_error",
                retry=True,
                task_number=prepared.task_number,
            )
        finally:
            self._authorization_cancellable = False

    async def _completion_worker(self):
        while not self._stopping:
            completion = await self.completion_queue.get()
            task = asyncio.create_task(self._complete(completion))
            self._completion_task = task
            try:
                await task
            except asyncio.CancelledError:
                if self._stopping:
                    raise
            finally:
                self._completion_cancellable = False
                self._completion_task = None
                self.completion_queue.task_done()

    async def _complete(self, completion):
        prepared = completion.prepared
        self._completion_cancellable = True
        try:
            async with asyncio.timeout(self.timeout):
                credential = await self.protocol.poll_token(
                    endpoint=prepared.flow.token_endpoint,
                    flow=prepared.flow,
                    timeout=self.timeout,
                )
        except asyncio.CancelledError:
            self._completion_cancellable = False
            await self._cancel_completion(prepared)
            raise
        except RuntimeError as error:
            self._completion_cancellable = False
            reason = str(error)
            mapping = {
                "oauth_denied": JobStatus.OAUTH_DENIED,
                "oauth_expired": JobStatus.OAUTH_EXPIRED,
                "oauth_rejected": JobStatus.OAUTH_REJECTED,
            }
            status = mapping.get(reason)
            if status is not None:
                await self._finish_failure(
                    prepared.source,
                    prepared.source_fingerprint,
                    prepared.job_id,
                    prepared.attempt_number,
                    status,
                    reason,
                    retry=False,
                    task_number=prepared.task_number,
                )
            else:
                await self._finish_failure(
                    prepared.source,
                    prepared.source_fingerprint,
                    prepared.job_id,
                    prepared.attempt_number,
                    JobStatus.TRANSPORT_FAILED,
                    "token_transport_failed",
                    retry=True,
                    task_number=prepared.task_number,
                )
            return
        except (httpx.TransportError, OSError, TimeoutError):
            self._completion_cancellable = False
            await self._finish_failure(
                prepared.source,
                prepared.source_fingerprint,
                prepared.job_id,
                prepared.attempt_number,
                JobStatus.TRANSPORT_FAILED,
                "token_transport_failed",
                retry=True,
                task_number=prepared.task_number,
            )
            return
        except Exception:
            self._completion_cancellable = False
            await self._finish_failure(
                prepared.source,
                prepared.source_fingerprint,
                prepared.job_id,
                prepared.attempt_number,
                JobStatus.TRANSPORT_FAILED,
                "token_failed",
                retry=True,
                task_number=prepared.task_number,
            )
            return

        if self.sink is None:
            self._completion_cancellable = False
            await self._finish_failure(
                prepared.source,
                prepared.source_fingerprint,
                prepared.job_id,
                prepared.attempt_number,
                JobStatus.SINK_FAILED,
                "sink_unconfigured",
                retry=False,
                task_number=prepared.task_number,
            )
            return
        # Sink persistence and the matching ledger transition form one commit.
        # LocalAuthFileSink uses a worker thread, so cancelling its await cannot
        # cancel the underlying atomic write and would otherwise cause a duplicate
        # authorization on retry.
        self._completion_cancellable = False
        store_task = asyncio.create_task(self.sink.store(credential))
        cancelled_during_commit = False
        try:
            try:
                receipt = await asyncio.shield(store_task)
            except asyncio.CancelledError:
                cancelled_during_commit = True
                receipt = await store_task
        except Exception:
            await self._finish_failure(
                prepared.source,
                prepared.source_fingerprint,
                prepared.job_id,
                prepared.attempt_number,
                JobStatus.SINK_FAILED,
                "sink_failed",
                retry=True,
                task_number=prepared.task_number,
            )
            return

        self._completion_cancellable = False
        self.ledger.finish(
            prepared.job_id,
            JobStatus.IMPORTED,
            "imported",
            receipt.fingerprint,
        )
        self._set_state(prepared.source_fingerprint, PipelineState.IMPORTED)
        self._transient_retries.pop(prepared.source_fingerprint, None)
        self._process_imports += 1
        self._recent_imports.append(self._clock())
        self._emit_result(
            JobStatus.IMPORTED,
            "imported",
            prepared.attempt_number,
            prepared.task_number,
        )
        if cancelled_during_commit:
            raise asyncio.CancelledError

    async def _cancel_completion(self, prepared):
        reason = "shutdown_cancelled" if self._stopping else "operator_cancelled"
        self.ledger.finish(prepared.job_id, JobStatus.CANCELLED, reason)
        if not self._stopping:
            await self._schedule_retry(
                prepared.source,
                prepared.source_fingerprint,
                self.CANCEL_RETRY_DELAY,
            )
            self._emit_result(
                JobStatus.CANCELLED,
                reason,
                prepared.attempt_number,
                prepared.task_number,
            )

    async def _finish_failure(
        self,
        source,
        fingerprint,
        job_id,
        attempt,
        status,
        reason,
        *,
        retry,
        task_number=None,
    ):
        self.ledger.finish(job_id, status, reason)
        if retry == "rate_limited":
            await self._schedule_retry(source, fingerprint, 0.0)
        elif retry:
            retry_count = self._transient_retries.get(fingerprint, 0)
            if retry_count < len(self.TRANSIENT_RETRY_DELAYS):
                self._transient_retries[fingerprint] = retry_count + 1
                await self._schedule_retry(
                    source,
                    fingerprint,
                    self.TRANSIENT_RETRY_DELAYS[retry_count],
                )
            else:
                self._set_state(fingerprint, PipelineState.TERMINAL)
        else:
            self._set_state(fingerprint, PipelineState.TERMINAL)
        self._emit_result(status, reason, attempt, task_number)

    def _prune_recent_imports(self):
        cutoff = self._clock() - 300.0
        while self._recent_imports and self._recent_imports[0] < cutoff:
            self._recent_imports.popleft()

    def _metrics(self):
        self._prune_recent_imports()
        counts = self.ledger.aggregate_counts()
        elapsed = max(1.0, self._clock() - self._started_monotonic)
        rolling_window = max(1.0, min(300.0, elapsed))
        finalized = counts["finalized_attempts"]
        attempted_unique = counts["attempted_unique"]
        return {
            **counts,
            **self.ledger.inventory_counts(),
            "attempt_success": (
                counts["imported_attempts"] / finalized if finalized else 0.0
            ),
            "eventual_success": (
                counts["imported_unique"] / attempted_unique
                if attempted_unique
                else 0.0
            ),
            "five_minute_imports_per_minute": (
                len(self._recent_imports) * 60.0 / rolling_window
                if self._process_imports
                else None
            ),
            "lifetime_imports_per_minute": (
                self._process_imports * 60.0 / elapsed
                if self._process_imports
                else None
            ),
            "pending_total": self._pending_total(),
        }

    def _pending_total(self):
        snapshot = getattr(self.source, "snapshot_fingerprints", None)
        if snapshot is None:
            return None
        return len(snapshot - self.ledger.imported_fingerprints())

    def _emit_result(self, status, reason, attempt, task_number=None):
        try:
            metrics = self._metrics()
        except Exception:
            self._emit(
                "pipeline_error", {"stage": "metrics", "reason": "internal_error"}
            )
            self.request_stop()
            return
        self._emit(
            "result",
            {
                "status": status.value,
                "reason": reason,
                "attempt_number": attempt,
                "task_number": task_number,
                **metrics,
            },
        )

    def status(self):
        active_stages = []
        if self._authorization_task is not None and not self._authorization_task.done():
            active_stages.append("authorization")
        if self._completion_task is not None and not self._completion_task.done():
            active_stages.append("completion")
        retry_waiting = sum(
            state is PipelineState.RETRY_WAITING for state in self._states.values()
        )
        next_retry_seconds = (
            round(max(0.0, self._retry_heap[0][0] - self._clock()), 1)
            if self._retry_heap
            else 0.0
        )
        return {
            "state": "stopping" if self._stopping else (
                "running" if self._resume.is_set() else "paused"
            ),
            "source_queue": self.source_queue.qsize(),
            "prepared_queue": self.prepared_queue.qsize(),
            "completion_queue": self.completion_queue.qsize(),
            "retry_waiting": retry_waiting,
            "next_retry_seconds": next_retry_seconds,
            "active_stage": "+".join(active_stages) if active_stages else "idle",
            "authorization_starts": self._authorization_starts,
            **self.rate_gate.snapshot(),
            **self.start_interval.snapshot(),
            **self._metrics(),
        }

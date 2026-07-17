import asyncio
from contextlib import suppress

import httpx

from .ledger import Ledger
from .models import (
    AuthorizationStatus,
    JobResult,
    JobStatus,
)


class EnrollmentCoordinator:
    def __init__(
        self,
        *,
        source,
        protocol,
        executor,
        sink,
        ledger_path,
        ledger_salt,
        concurrency=1,
        timeout=1800,
        retry_attempts=0,
        event_callback=None,
    ):
        self.source = source
        self.protocol = protocol
        self.executor = executor
        self.sink = sink
        self.ledger = Ledger(ledger_path, ledger_salt)
        self.concurrency = concurrency
        self.timeout = timeout
        self.retry_attempts = max(0, retry_attempts)
        self.event_callback = event_callback
        self._semaphore = asyncio.Semaphore(concurrency)

    @staticmethod
    def _is_pre_device_transport_failure(error):
        return isinstance(error, (httpx.TransportError, OSError))

    def _emit(self, kind, data):
        if self.event_callback is None:
            return
        try:
            self.event_callback(kind, data)
        except Exception:
            pass

    async def run(self, target=1):
        return await self.run_records(self.source.records(), target=target)

    async def run_records(self, records, target=100):
        if not 1 <= target <= 100:
            raise ValueError("target must be between 1 and 100")
        self.ledger.recover_pending()
        results = []
        tasks = []
        index = 0
        if hasattr(records, "__aiter__"):
            async for source in records:
                if index >= target:
                    break
                index += 1
                tasks.append(asyncio.create_task(self._run_one(source)))
        else:
            for source in records:
                if index >= target:
                    break
                index += 1
                tasks.append(asyncio.create_task(self._run_one(source)))
        if hasattr(self.executor, "start"):
            await self.executor.start()
        try:
            if tasks:
                results = await asyncio.gather(*tasks)
            return results
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if hasattr(self.executor, "close"):
                with suppress(Exception):
                    await self.executor.close()

    async def _run_one(self, source):
        async with self._semaphore:
            for attempt in range(1, self.retry_attempts + 2):
                job_id = self.ledger.start(source.source_id, attempt=attempt)
                try:
                    async with asyncio.timeout(self.timeout):
                        try:
                            flow = await self.protocol.start_device_flow()
                        except Exception as error:
                            result = JobResult(
                                source.source_id,
                                JobStatus.TRANSPORT_FAILED,
                                "device_flow_failed",
                                attempt,
                            )
                            self.ledger.finish(job_id, result.status, result.reason_code)
                            if (
                                self._is_pre_device_transport_failure(error)
                                and attempt <= self.retry_attempts
                            ):
                                continue
                            return result
                        self._emit(
                            "device_flow",
                            {
                                "source_id": source.source_id,
                                "user_code": flow.user_code,
                                "verification_url": flow.verification_url,
                            },
                        )
                        return await self._attempt_after_flow(source, job_id, flow, attempt)
                except asyncio.TimeoutError:
                    result = JobResult(source.source_id, JobStatus.TIMEOUT, "timeout", attempt)
                    self.ledger.finish(job_id, result.status, result.reason_code)
                    return result
                except asyncio.CancelledError:
                    result = JobResult(source.source_id, JobStatus.CANCELLED, "cancelled", attempt)
                    self.ledger.finish(job_id, result.status, result.reason_code)
                    raise
                except Exception:
                    result = JobResult(
                        source.source_id, JobStatus.TRANSPORT_FAILED, "transport_failed", attempt
                    )
                    self.ledger.finish(job_id, result.status, result.reason_code)
                    return result

    async def _attempt_after_flow(self, source, job_id, flow, attempt):
        authorization = await self.executor.confirm(source, flow)
        if authorization.status is not AuthorizationStatus.AUTHORIZED:
            status = JobStatus(authorization.status.value)
            result = JobResult(source.source_id, status, authorization.reason_code, attempt)
            self.ledger.finish(job_id, result.status, result.reason_code)
            return result
        if self.sink is None:
            result = JobResult(source.source_id, JobStatus.SINK_FAILED, "sink_unconfigured", attempt)
            self.ledger.finish(job_id, result.status, result.reason_code)
            return result
        try:
            credential = await self.protocol.poll_token(
                endpoint=flow.token_endpoint,
                flow=flow,
                timeout=self.timeout,
            )
        except RuntimeError as error:
            reason = str(error)
            mapping = {
                "oauth_denied": JobStatus.OAUTH_DENIED,
                "oauth_expired": JobStatus.OAUTH_EXPIRED,
                "oauth_rejected": JobStatus.OAUTH_REJECTED,
            }
            result = JobResult(
                source.source_id, mapping.get(reason, JobStatus.TRANSPORT_FAILED), reason, attempt
            )
            self.ledger.finish(job_id, result.status, result.reason_code)
            return result
        try:
            receipt = await self.sink.store(credential)
        except Exception:
            result = JobResult(source.source_id, JobStatus.SINK_FAILED, "sink_failed", attempt)
            self.ledger.finish(job_id, result.status, result.reason_code)
            return result
        result = JobResult(
            source.source_id,
            JobStatus.IMPORTED,
            "imported",
            attempt,
            sink_receipt_fingerprint=receipt.fingerprint,
        )
        self.ledger.finish(job_id, result.status, result.reason_code, receipt.fingerprint)
        return result

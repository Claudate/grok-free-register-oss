import asyncio
import threading

from xai_enroller.auth_pipeline import AuthPipeline
from xai_enroller.ledger import Ledger
from xai_enroller.models import (
    AuthorizationResult,
    AuthorizationStatus,
    DeviceFlow,
    JobStatus,
    OAuthCredential,
    PipelineState,
    SinkReceipt,
    SourceRecord,
)


class EmptySource:
    async def records(self):
        if False:
            yield None

    async def close(self):
        return None


class NoopExecutor:
    async def close(self):
        return None


def _pipeline(tmp_path):
    return AuthPipeline(
        source=EmptySource(),
        protocol=object(),
        executor=NoopExecutor(),
        sink=object(),
        ledger=Ledger(tmp_path / "ledger.db", b"salt"),
    )


def test_cancel_active_is_idempotent_while_cleanup_is_in_progress(tmp_path):
    async def scenario():
        pipeline = _pipeline(tmp_path)

        async def wait_forever():
            await asyncio.Future()

        task = asyncio.create_task(wait_forever())
        pipeline._authorization_task = task
        pipeline._authorization_cancellable = True
        assert await pipeline.cancel_active()
        assert not await pipeline.cancel_active()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


class SingleSource:
    async def records(self):
        yield SourceRecord("stock", "opaque")

    async def close(self):
        return None


class ImmediateProtocol:
    async def start_device_flow(self):
        return DeviceFlow(
            "device",
            "code",
            "https://accounts.x.ai/oauth2/device",
            600,
            0,
        )

    async def poll_token(self, **_kwargs):
        return OAuthCredential(
            "access",
            "refresh",
            None,
            "Bearer",
            3600,
            "later",
            "now",
            "subject",
            "https://auth.x.ai/oauth2/token",
        )


class ImmediateExecutor:
    def __init__(self):
        self.calls = 0

    async def start(self):
        return None

    async def close(self):
        return None

    async def confirm(self, *_args):
        self.calls += 1
        return AuthorizationResult(AuthorizationStatus.AUTHORIZED, "confirmed")


class BlockingThreadSink:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.pipeline = None
        self.calls = 0

    def _store(self):
        self.calls += 1
        self.entered.set()
        self.release.wait(2)
        return SinkReceipt("opaque")

    async def store(self, _credential):
        receipt = await asyncio.to_thread(self._store)
        self.pipeline.request_stop()
        return receipt


def test_sink_and_ledger_commit_cannot_be_cancelled_mid_commit(tmp_path):
    async def scenario():
        executor = ImmediateExecutor()
        sink = BlockingThreadSink()
        ledger = Ledger(tmp_path / "ledger.db", b"salt")
        events = []
        pipeline = AuthPipeline(
            source=SingleSource(),
            protocol=ImmediateProtocol(),
            executor=executor,
            sink=sink,
            ledger=ledger,
            timeout=2,
            event_callback=lambda kind, data: events.append((kind, data)),
        )
        sink.pipeline = pipeline
        run = asyncio.create_task(pipeline.run())
        assert await asyncio.to_thread(sink.entered.wait, 1)
        assert not await pipeline.cancel_active()
        sink.release.set()
        await asyncio.wait_for(run, 2)
        assert executor.calls == 1
        assert sink.calls == 1
        assert ledger.aggregate_counts()["imported_unique"] == 1
        started = next(data for kind, data in events if kind == "authorization_started")
        imported = next(
            data
            for kind, data in events
            if kind == "result" and data["status"] == "imported"
        )
        assert imported["task_number"] == started["task_number"]

    asyncio.run(scenario())


def test_pending_total_uses_current_snapshot_set_difference(tmp_path):
    class SnapshotSource(EmptySource):
        snapshot_fingerprints = frozenset({"a", "b", "c"})

    ledger = Ledger(tmp_path / "ledger.db", b"salt")
    imported = ledger.start_fingerprint("b")
    ledger.finish(imported, JobStatus.IMPORTED, "imported", "receipt-b")
    outside = ledger.start_fingerprint("outside")
    ledger.finish(outside, JobStatus.IMPORTED, "imported", "receipt-outside")
    pipeline = AuthPipeline(
        source=SnapshotSource(),
        protocol=object(),
        executor=NoopExecutor(),
        sink=object(),
        ledger=ledger,
    )

    assert pipeline.status()["pending_total"] == 2
    assert pipeline.status()["five_minute_imports_per_minute"] is None
    assert pipeline.status()["lifetime_imports_per_minute"] is None


def test_pending_total_is_unknown_before_first_valid_snapshot(tmp_path):
    pipeline = _pipeline(tmp_path)

    assert pipeline.status()["pending_total"] is None


def test_rate_limit_gate_grows_cooldown_on_consecutive_trips():
    class Clock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

    async def scenario():
        from xai_enroller.auth_pipeline import GlobalRateLimitGate

        clock = Clock()
        gate = GlobalRateLimitGate(clock=clock, base_cooldown=60.0)
        assert await gate.rate_limited()
        assert gate.COOLDOWN_SECONDS == 60.0
        assert gate.snapshot()["cooldown_seconds"] == 60.0

        resume = asyncio.Event()
        resume.set()
        clock.now = 60.0
        probe = await gate.wait_for_permission(resume)
        assert probe is not None
        assert await gate.rate_limited(probe)
        assert gate.COOLDOWN_SECONDS == 90.0
        assert gate.snapshot()["cooldown_remaining_seconds"] == 90.0

        clock.now = 150.0
        probe = await gate.wait_for_permission(resume)
        elapsed = await gate.authorized(probe)
        assert elapsed == 150.0
        assert gate.COOLDOWN_SECONDS == 60.0
        assert gate.snapshot()["rate_limit_trips"] == 0

    asyncio.run(scenario())


def test_minimum_start_interval_raises_after_rate_limit_and_decays():
    from xai_enroller.auth_pipeline import MinimumStartInterval

    def approx(value, rel=1e-6):
        class Approx:
            def __eq__(self, other):
                return abs(float(other) - float(value)) <= rel * max(1.0, abs(value))

            def __repr__(self):
                return f"approx({value})"

        return Approx()

    interval = MinimumStartInterval(10.0)
    assert interval.seconds == 10.0
    raised = interval.note_rate_limited()
    assert raised == 18.0
    raised = interval.note_rate_limited()
    assert raised == approx(18.0 * 1.55)
    for _ in range(MinimumStartInterval.SUCCESS_STREAK_TO_DECAY):
        interval.note_success()
    assert interval.seconds < raised
    # Keep decaying until the base floor is restored.
    for _ in range(40):
        interval.note_success()
    assert interval.seconds == 10.0


def test_queued_source_imported_while_paused_is_not_authorized(tmp_path):
    async def scenario():
        executor = ImmediateExecutor()
        ledger = Ledger(tmp_path / "ledger.db", b"salt")
        pipeline = AuthPipeline(
            source=EmptySource(),
            protocol=ImmediateProtocol(),
            executor=executor,
            sink=object(),
            ledger=ledger,
            timeout=2,
        )
        source = SourceRecord("stock", "opaque")
        fingerprint = ledger.fingerprint(source.source_id)

        assert await pipeline.admit(source)
        pipeline.pause()
        await pipeline.rate_gate.rate_limited()
        run = asyncio.create_task(pipeline.run())
        try:
            async with asyncio.timeout(1):
                while pipeline._authorization_task is None:
                    await asyncio.sleep(0)

            external_job_id = ledger.start_fingerprint(fingerprint)
            ledger.finish(
                external_job_id,
                JobStatus.IMPORTED,
                "imported",
                "external-receipt",
            )
            pipeline.resume()

            async with asyncio.timeout(1):
                while (
                    executor.calls == 0
                    and pipeline._states.get(fingerprint) is not PipelineState.IMPORTED
                ):
                    await asyncio.sleep(0)

            assert executor.calls == 0
            assert pipeline._states[fingerprint] is PipelineState.IMPORTED
        finally:
            pipeline.request_stop()
            await asyncio.wait_for(run, 2)

    asyncio.run(scenario())

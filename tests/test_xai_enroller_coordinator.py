import asyncio
import sqlite3

import pytest

from xai_enroller.coordinator import EnrollmentCoordinator
from xai_enroller.models import (
    AuthorizationResult,
    AuthorizationStatus,
    DeviceFlow,
    JobStatus,
    OAuthCredential,
    SinkReceipt,
    SourceRecord,
)
from xai_enroller.sources import FileSourceAdapter, SQLiteSourceAdapter


class Source:
    async def records(self):
        for index in range(3):
            yield SourceRecord(f"source-{index}", f"token-{index}")


class Protocol:
    def __init__(self):
        self.polls = 0

    async def start_device_flow(self):
        return DeviceFlow("device", "code", "https://accounts.x.ai/device", 60, 0)

    async def poll_token(self, **kwargs):
        self.polls += 1
        return OAuthCredential(
            "access",
            "refresh",
            None,
            "Bearer",
            3600,
            "2026-07-11T00:00:00Z",
            "2026-07-11T00:00:00Z",
            "subject",
            "https://auth.x.ai/oauth2/token",
        )


class Executor:
    active = 0
    max_active = 0

    async def confirm(self, source, flow):
        type(self).active += 1
        type(self).max_active = max(type(self).max_active, type(self).active)
        await asyncio.sleep(0)
        type(self).active -= 1
        if source.source_id == "source-1":
            return AuthorizationResult(AuthorizationStatus.NEEDS_BROWSER, "challenge")
        return AuthorizationResult(AuthorizationStatus.AUTHORIZED, "confirmed")


class Sink:
    async def store(self, credential):
        if credential.subject == "subject":
            return SinkReceipt("receipt")


class FlakyProtocol(Protocol):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def start_device_flow(self):
        self.calls += 1
        if self.calls == 1:
            raise OSError("temporary transport")
        return await super().start_device_flow()


class InvalidProtocol(Protocol):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def start_device_flow(self):
        self.calls += 1
        raise ValueError("invalid device response")


class FailingExecutor(Executor):
    async def confirm(self, source, flow):
        raise OSError("confirmation transport")


class SlowDeviceFlowProtocol(Protocol):
    async def start_device_flow(self):
        await asyncio.sleep(0.04)
        return await super().start_device_flow()


class SlowExecutor(Executor):
    async def confirm(self, source, flow):
        await asyncio.sleep(0.04)
        return await super().confirm(source, flow)


def test_coordinator_limits_concurrency_and_maps_terminal_results(tmp_path):
    protocol = Protocol()
    coordinator = EnrollmentCoordinator(
        source=Source(),
        protocol=protocol,
        executor=Executor(),
        sink=Sink(),
        ledger_path=tmp_path / "ledger.db",
        ledger_salt=b"salt",
        concurrency=2,
        timeout=5,
    )
    results = asyncio.run(coordinator.run(target=3))
    assert [result.status for result in results] == [
        JobStatus.IMPORTED,
        JobStatus.NEEDS_BROWSER,
        JobStatus.IMPORTED,
    ]
    assert Executor.max_active == 2
    assert protocol.polls == 2


def test_coordinator_accepts_explicit_synced_records(tmp_path):
    coordinator = EnrollmentCoordinator(
        source=Source(),
        protocol=Protocol(),
        executor=Executor(),
        sink=Sink(),
        ledger_path=tmp_path / "ledger.db",
        ledger_salt=b"salt",
        concurrency=1,
        timeout=5,
    )

    results = asyncio.run(
        coordinator.run_records([SourceRecord("synced-account", "synced-sso")])
    )

    assert [result.source_id for result in results] == ["synced-account"]
    assert results[0].status is JobStatus.IMPORTED


def test_coordinator_emits_device_flow_after_the_flow_is_created(tmp_path):
    events = []
    coordinator = EnrollmentCoordinator(
        source=Source(),
        protocol=Protocol(),
        executor=Executor(),
        sink=Sink(),
        ledger_path=tmp_path / "ledger.db",
        ledger_salt=b"salt",
        concurrency=1,
        timeout=5,
        event_callback=lambda kind, data: events.append((kind, data)),
    )

    asyncio.run(coordinator.run_records([SourceRecord("synced-account", "synced-sso")]))

    assert events == [
        (
            "device_flow",
            {
                "source_id": "synced-account",
                "user_code": "code",
                "verification_url": "https://accounts.x.ai/device",
            },
        )
    ]


def test_coordinator_without_sink_never_reports_imported(tmp_path):
    coordinator = EnrollmentCoordinator(
        source=Source(),
        protocol=Protocol(),
        executor=Executor(),
        sink=None,
        ledger_path=tmp_path / "ledger.db",
        ledger_salt=b"salt",
        concurrency=1,
        timeout=5,
    )
    results = asyncio.run(coordinator.run(target=1))
    assert results[0].status is JobStatus.SINK_FAILED


def test_coordinator_retries_only_before_device_flow_creation(tmp_path):
    protocol = FlakyProtocol()
    coordinator = EnrollmentCoordinator(
        source=Source(),
        protocol=protocol,
        executor=Executor(),
        sink=Sink(),
        ledger_path=tmp_path / "ledger.db",
        ledger_salt=b"salt",
        concurrency=1,
        timeout=5,
        retry_attempts=1,
    )
    results = asyncio.run(coordinator.run(target=1))
    assert results[0].status is JobStatus.IMPORTED
    assert protocol.calls == 2


def test_coordinator_does_not_retry_after_device_flow_creation(tmp_path):
    protocol = Protocol()
    coordinator = EnrollmentCoordinator(
        source=Source(),
        protocol=protocol,
        executor=FailingExecutor(),
        sink=Sink(),
        ledger_path=tmp_path / "ledger.db",
        ledger_salt=b"salt",
        concurrency=1,
        timeout=5,
        retry_attempts=3,
    )
    results = asyncio.run(coordinator.run(target=1))
    assert results[0].status is JobStatus.TRANSPORT_FAILED
    assert protocol.polls == 0


def test_coordinator_does_not_retry_non_transport_device_flow_error(tmp_path):
    protocol = InvalidProtocol()
    coordinator = EnrollmentCoordinator(
        source=Source(),
        protocol=protocol,
        executor=Executor(),
        sink=Sink(),
        ledger_path=tmp_path / "ledger.db",
        ledger_salt=b"salt",
        concurrency=1,
        timeout=5,
        retry_attempts=3,
    )
    results = asyncio.run(coordinator.run(target=1))
    assert results[0].status is JobStatus.TRANSPORT_FAILED
    assert protocol.calls == 1


def test_coordinator_applies_timeout_to_the_entire_attempt(tmp_path):
    coordinator = EnrollmentCoordinator(
        source=Source(),
        protocol=SlowDeviceFlowProtocol(),
        executor=SlowExecutor(),
        sink=Sink(),
        ledger_path=tmp_path / "ledger.db",
        ledger_salt=b"salt",
        concurrency=1,
        timeout=0.06,
        retry_attempts=3,
    )

    results = asyncio.run(coordinator.run(target=1))

    assert results[0].status is JobStatus.TIMEOUT


@pytest.mark.parametrize("source_kind", ["file", "sqlite"])
def test_coordinator_consumes_real_source_adapters(tmp_path, source_kind):
    if source_kind == "file":
        source_path = tmp_path / "sessions.tsv"
        source_path.write_text("file-source\tfile-token\n", encoding="utf-8")
        source_path.chmod(0o600)
        source = FileSourceAdapter(source_path)
    else:
        source_path = tmp_path / "accounts.db"
        connection = sqlite3.connect(source_path)
        connection.execute(
            "CREATE TABLE accounts (token TEXT, status TEXT, deleted_at TEXT, updated_at INTEGER)"
        )
        connection.execute("INSERT INTO accounts VALUES (?, ?, ?, ?)", ("db-token", "active", None, 1))
        connection.commit()
        connection.close()
        source = SQLiteSourceAdapter(source_path, b"source-salt")

    coordinator = EnrollmentCoordinator(
        source=source,
        protocol=Protocol(),
        executor=Executor(),
        sink=Sink(),
        ledger_path=tmp_path / f"{source_kind}-ledger.db",
        ledger_salt=b"ledger-salt",
        concurrency=1,
        timeout=5,
    )
    results = asyncio.run(coordinator.run(target=1))
    assert len(results) == 1
    assert results[0].status is JobStatus.IMPORTED

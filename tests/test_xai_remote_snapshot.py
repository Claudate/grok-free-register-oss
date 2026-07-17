import asyncio
import json
import os

from xai_enroller.models import SourceRecord
from xai_enroller.remote_stream import (
    DiskSnapshotSource,
    LocalSnapshotSynchronizer,
    SSHSnapshotSynchronizer,
)


def _document(source_id):
    return json.dumps(
        {
            "email": source_id,
            "cookies": [
                {
                    "name": "sso",
                    "value": "opaque",
                    "domain": "accounts.x.ai",
                    "path": "/",
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


class FakeStream:
    def __init__(self, lines):
        self._lines = iter(lines)

    async def readline(self):
        return next(self._lines, b"")

    async def read(self, _size=-1):
        return b""


class FakeProcess:
    def __init__(self, lines, returncode=0):
        self.stdout = FakeStream(lines)
        self.stderr = FakeStream([])
        self.returncode = None
        self._final_returncode = returncode

    async def wait(self):
        self.returncode = self._final_returncode
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def test_snapshot_sync_atomically_replaces_only_valid_complete_export(tmp_path):
    async def scenario():
        destination = tmp_path / "source-snapshot.jsonl"
        destination.write_bytes(_document("old") + b"\n")

        invalid = FakeProcess([b"not-json\n"])

        async def invalid_factory(*_args, **_kwargs):
            return invalid

        synchronizer = SSHSnapshotSynchronizer(
            "host",
            destination,
            process_factory=invalid_factory,
            fingerprint=lambda source_id: f"key:{source_id}",
        )
        assert not await synchronizer.sync_once()
        assert destination.read_bytes() == _document("old") + b"\n"

        rejected = FakeProcess([_document("rejected") + b"\n"], returncode=1)

        async def rejected_factory(*_args, **_kwargs):
            return rejected

        synchronizer.process_factory = rejected_factory
        assert not await synchronizer.sync_once()
        assert destination.read_bytes() == _document("old") + b"\n"

        empty = FakeProcess([])

        async def empty_factory(*_args, **_kwargs):
            return empty

        synchronizer.process_factory = empty_factory
        assert not await synchronizer.sync_once()
        assert destination.read_bytes() == _document("old") + b"\n"

        valid = FakeProcess([_document("new-a") + b"\n", _document("new-b") + b"\n"])

        async def valid_factory(*_args, **_kwargs):
            return valid

        synchronizer.process_factory = valid_factory
        assert await synchronizer.sync_once()
        assert synchronizer.snapshot_fingerprints == frozenset(
            {"key:new-a", "key:new-b"}
        )
        assert destination.read_bytes() == _document("new-a") + b"\n" + _document("new-b") + b"\n"
        assert destination.stat().st_mode & 0o777 == 0o600
        assert not list(tmp_path.glob(".source-snapshot.jsonl.*.tmp"))

        synchronizer.process_factory = invalid_factory
        assert not await synchronizer.sync_once()
        assert synchronizer.snapshot_fingerprints == frozenset(
            {"key:new-a", "key:new-b"}
        )

    asyncio.run(scenario())


def test_local_snapshot_accepts_empty_input_and_anchors_exporter_code(tmp_path):
    async def scenario():
        registration_root = tmp_path / "registration"
        destination = tmp_path / "auth" / "source-snapshot.jsonl"
        captured = []
        process = FakeProcess([])

        async def factory(*args, **_kwargs):
            captured.append(args)
            return process

        synchronizer = LocalSnapshotSynchronizer(
            registration_root,
            destination,
            process_factory=factory,
        )

        assert await synchronizer.sync_once()
        assert destination.read_bytes() == b""
        assert destination.stat().st_mode & 0o777 == 0o600
        assert synchronizer.snapshot_fingerprints == frozenset()
        assert captured[0][0]
        assert captured[0][1] != str(registration_root / "scripts" / "export_registered_sessions.py")
        assert captured[0][-2:] == (
            str(registration_root / "keys" / "auth-sessions.jsonl"),
            str(registration_root / "keys" / "accounts.txt"),
        )

    asyncio.run(scenario())


def test_local_snapshot_rejects_candidate_when_either_input_changes(tmp_path):
    async def scenario():
        registration_root = tmp_path / "registration"
        keys = registration_root / "keys"
        keys.mkdir(parents=True)
        sessions = keys / "auth-sessions.jsonl"
        accounts = keys / "accounts.txt"
        sessions.write_bytes(_document("old") + b"\n")
        accounts.write_text("", encoding="utf-8")
        destination = tmp_path / "auth" / "source-snapshot.jsonl"
        destination.parent.mkdir()
        destination.write_bytes(_document("stable") + b"\n")

        process = FakeProcess([_document("candidate") + b"\n"])

        async def factory(*_args, **_kwargs):
            accounts.write_text("new@example.test:p:sso\n", encoding="utf-8")
            return process

        synchronizer = LocalSnapshotSynchronizer(
            registration_root,
            destination,
            process_factory=factory,
        )

        assert not await synchronizer.sync_once()
        assert destination.read_bytes() == _document("stable") + b"\n"

    asyncio.run(scenario())


def test_snapshot_consumer_finishes_open_generation_before_replacement(tmp_path):
    async def scenario():
        destination = tmp_path / "source-snapshot.jsonl"
        destination.write_bytes(_document("old-a") + b"\n" + _document("old-b") + b"\n")
        source = DiskSnapshotSource(destination, poll_seconds=0.01)
        records = source.records()
        first = await anext(records)
        assert isinstance(first, SourceRecord)
        assert first.source_id == "old-a"

        replacement = tmp_path / "replacement"
        replacement.write_bytes(_document("new-a") + b"\n")
        os.replace(replacement, destination)

        assert (await anext(records)).source_id == "old-b"
        assert (await asyncio.wait_for(anext(records), 1)).source_id == "new-a"
        await source.close()
        await records.aclose()

    asyncio.run(scenario())


def test_snapshot_source_reports_only_aggregate_new_records(tmp_path):
    class Synchronizer:
        def __init__(self):
            self.snapshot_fingerprints = None
            self.calls = 0
            self.source = None

        async def sync_once(self):
            self.calls += 1
            if self.calls == 1:
                self.snapshot_fingerprints = frozenset({"a", "b"})
            else:
                self.snapshot_fingerprints = frozenset({"a", "b", "c"})
                self.source._closed = True
            return True

        async def close(self):
            return None

    async def scenario():
        synchronizer = Synchronizer()
        events = []
        source = DiskSnapshotSource(
            tmp_path / "source-snapshot.jsonl",
            synchronizer=synchronizer,
            sync_seconds=0.001,
            event_callback=lambda kind, data: events.append((kind, data)),
        )
        synchronizer.source = source

        await source._sync_loop()

        assert events == [
            ("source_connected", {}),
            ("source_updated", {"new": 1, "total": 3}),
        ]

    asyncio.run(scenario())

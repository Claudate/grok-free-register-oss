import sqlite3

import pytest

from xai_enroller.ledger import Ledger
from xai_enroller.models import JobStatus


def test_ledger_persists_only_redacted_terminal_fields(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = Ledger(path, b"salt")
    job_id = ledger.start("source", attempt=1)
    ledger.finish(job_id, JobStatus.SINK_FAILED, "sink_failed", "receipt")
    raw = path.read_bytes()
    for secret in [
        "sso-token",
        "device-code",
        "https://accounts.x.ai",
        "access-token",
        "refresh-token",
        "id-token",
        "person@example.com",
    ]:
        assert secret.encode() not in raw
    row = ledger.get(job_id)
    assert row["status"] == "sink_failed"
    assert row["reason_code"] == "sink_failed"
    assert row["sink_receipt_fingerprint"] == "receipt"
    assert "source" not in repr(row["source_fingerprint"])


def test_ledger_recovers_pending_jobs(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db", b"salt")
    job_id = ledger.start("source", attempt=1)
    ledger.recover_pending()
    assert ledger.get(job_id)["status"] == JobStatus.CANCELLED.value


def test_ledger_backfills_imported_receipts_into_available_inventory(tmp_path):
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                job_id INTEGER PRIMARY KEY,
                source_fingerprint TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                reason_code TEXT,
                sink_receipt_fingerprint TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO jobs(source_fingerprint, attempt_number, status, started_at, "
            "finished_at, reason_code, sink_receipt_fingerprint) VALUES (?, 1, ?, ?, ?, ?, ?)",
            [
                ("source-old", "imported", "2026-01-01", "2026-01-02", "imported", "receipt-old"),
                ("source-new", "imported", "2026-01-03", "2026-01-04", "imported", "receipt-new"),
                ("source-failed", "sink_failed", "2026-01-05", "2026-01-06", "sink_failed", "receipt-failed"),
            ],
        )

    ledger = Ledger(path, b"salt")

    assert ledger.inventory_counts() == {
        "available": 2,
        "claiming": 0,
        "claimed": 0,
    }
    assert ledger.claim_available(2, "batch-backfill") == [
        "receipt-new",
        "receipt-old",
    ]


def test_imported_finish_and_inventory_insert_are_atomic(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db", b"salt")
    job_id = ledger.start("source", attempt=1)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_inventory_insert
            BEFORE INSERT ON credential_inventory
            BEGIN
                SELECT RAISE(ABORT, 'inventory insert rejected');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="inventory insert rejected"):
        ledger.finish(job_id, JobStatus.IMPORTED, "imported", "receipt")

    assert ledger.get(job_id)["status"] == "pending"
    assert ledger.inventory_counts()["available"] == 0


def test_claim_available_uses_latest_finish_and_tracks_batch(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db", b"salt")
    jobs = []
    for source, receipt in [
        ("source-a", "receipt-a"),
        ("source-b", "receipt-b"),
        ("source-c", "receipt-c"),
    ]:
        job_id = ledger.start(source)
        ledger.finish(job_id, JobStatus.IMPORTED, "imported", receipt)
        jobs.append(job_id)
    with sqlite3.connect(ledger.path) as connection:
        connection.executemany(
            "UPDATE jobs SET finished_at=? WHERE job_id=?",
            [
                ("2026-01-01T00:00:00+00:00", jobs[0]),
                ("2026-01-03T00:00:00+00:00", jobs[1]),
                ("2026-01-02T00:00:00+00:00", jobs[2]),
            ],
        )

    assert ledger.claim_available(2, "batch-1") == ["receipt-b", "receipt-c"]
    assert ledger.claim_available(2, "batch-2") == ["receipt-a"]
    assert ledger.pending_claims("batch-1") == [
        {
            "sink_receipt_fingerprint": "receipt-b",
            "batch_id": "batch-1",
            "note": "",
        },
        {
            "sink_receipt_fingerprint": "receipt-c",
            "batch_id": "batch-1",
            "note": "",
        },
    ]
    assert ledger.inventory_counts() == {
        "available": 0,
        "claiming": 3,
        "claimed": 0,
    }


def test_claim_completion_and_recovery_are_batch_scoped(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db", b"salt")
    for source, receipt in [
        ("source-a", "receipt-a"),
        ("source-b", "receipt-b"),
        ("source-c", "receipt-c"),
    ]:
        job_id = ledger.start(source)
        ledger.finish(job_id, JobStatus.IMPORTED, "imported", receipt)

    assert ledger.claim_available(2, "batch-complete")
    assert ledger.claim_available(1, "batch-recover")
    assert ledger.mark_claimed("batch-complete", "delivered") == 2
    assert ledger.recover_claiming("batch-recover", note="worker_restart") == 1

    assert ledger.pending_claims() == []
    assert ledger.inventory_counts() == {
        "available": 1,
        "claiming": 0,
        "claimed": 2,
    }
    assert ledger.claim_available(1, "batch-retry") == ["receipt-a"]

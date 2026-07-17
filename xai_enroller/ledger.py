import hashlib
import hmac
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import JobStatus


class Ledger:
    def __init__(self, path: Path, salt: bytes):
        self.path = Path(path)
        self.salt = bytes(salt)
        self._init()

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
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
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
            }
            if "authorization_started" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN authorization_started INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_source_status_idx "
                "ON jobs(source_fingerprint, status)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS credential_inventory (
                    sink_receipt_fingerprint TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK(state IN ('available', 'claiming', 'claimed')),
                    claimed_at TEXT NOT NULL DEFAULT '',
                    batch_id TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS credential_inventory_state_idx "
                "ON credential_inventory(state)"
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO credential_inventory(
                    sink_receipt_fingerprint, state
                )
                SELECT sink_receipt_fingerprint, 'available'
                FROM jobs
                WHERE status=?
                  AND COALESCE(TRIM(sink_receipt_fingerprint), '') <> ''
                """,
                (JobStatus.IMPORTED.value,),
            )
        os.chmod(self.path, 0o600)

    def fingerprint(self, source_id):
        return hmac.new(self.salt, source_id.encode(), hashlib.sha256).hexdigest()

    def _fingerprint(self, source_id):
        return self.fingerprint(source_id)

    def start(self, source_id, attempt=1):
        return self.start_fingerprint(self.fingerprint(source_id), attempt=attempt)

    def start_fingerprint(self, source_fingerprint, attempt=None):
        if attempt is None:
            attempt = self.next_attempt(source_fingerprint)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO jobs(source_fingerprint, attempt_number, status, started_at) "
                "VALUES (?, ?, ?, ?)",
                (source_fingerprint, attempt, "pending", now),
            )
            return cursor.lastrowid

    def next_attempt(self, source_fingerprint):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS next_attempt "
                "FROM jobs WHERE source_fingerprint=?",
                (source_fingerprint,),
            ).fetchone()
        return int(row["next_attempt"])

    def mark_authorization_started(self, job_id):
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET authorization_started=1 "
                "WHERE job_id=? AND status='pending'",
                (job_id,),
            )

    def finish(self, job_id, status, reason_code, sink_receipt_fingerprint=None):
        status_value = status.value if isinstance(status, JobStatus) else str(status)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status=?, finished_at=?, reason_code=?, "
                "sink_receipt_fingerprint=? "
                "WHERE job_id=? AND status='pending'",
                (status_value, now, reason_code, sink_receipt_fingerprint, job_id),
            )
            if (
                cursor.rowcount == 1
                and status_value == JobStatus.IMPORTED.value
                and sink_receipt_fingerprint
            ):
                connection.execute(
                    "INSERT OR IGNORE INTO credential_inventory("
                    "sink_receipt_fingerprint, state) VALUES (?, 'available')",
                    (sink_receipt_fingerprint,),
                )

    def claim_available(self, limit, batch_id):
        limit = int(limit)
        if limit <= 0:
            return []
        batch_id = str(batch_id).strip()
        if not batch_id:
            raise ValueError("batch_id must not be empty")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                WITH latest_import AS (
                    SELECT sink_receipt_fingerprint, MAX(finished_at) AS finished_at
                    FROM jobs
                    WHERE status=?
                      AND COALESCE(TRIM(sink_receipt_fingerprint), '') <> ''
                    GROUP BY sink_receipt_fingerprint
                )
                SELECT inventory.sink_receipt_fingerprint
                FROM credential_inventory AS inventory
                JOIN latest_import USING (sink_receipt_fingerprint)
                WHERE inventory.state='available'
                ORDER BY latest_import.finished_at DESC,
                         inventory.sink_receipt_fingerprint ASC
                LIMIT ?
                """,
                (JobStatus.IMPORTED.value, limit),
            ).fetchall()
            fingerprints = [row["sink_receipt_fingerprint"] for row in rows]
            connection.executemany(
                "UPDATE credential_inventory SET state='claiming', batch_id=?, "
                "claimed_at='', note='' "
                "WHERE sink_receipt_fingerprint=? AND state='available'",
                ((batch_id, fingerprint) for fingerprint in fingerprints),
            )
        return fingerprints

    def mark_claimed(self, batch_id, note=""):
        batch_id = str(batch_id).strip()
        if not batch_id:
            raise ValueError("batch_id must not be empty")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE credential_inventory SET state='claimed', claimed_at=?, note=? "
                "WHERE state='claiming' AND batch_id=?",
                (datetime.now(timezone.utc).isoformat(), str(note), batch_id),
            )
            return cursor.rowcount

    def inventory_counts(self):
        counts = {"available": 0, "claiming": 0, "claimed": 0}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM credential_inventory GROUP BY state"
            )
            for row in rows:
                counts[row["state"]] = int(row["count"])
        return counts

    def pending_claims(self, batch_id=None):
        query = (
            "SELECT sink_receipt_fingerprint, batch_id, note "
            "FROM credential_inventory WHERE state='claiming'"
        )
        params = ()
        if batch_id is not None:
            query += " AND batch_id=?"
            params = (str(batch_id),)
        query += " ORDER BY batch_id, sink_receipt_fingerprint"
        with self._connect() as connection:
            rows = connection.execute(query, params)
            return [dict(row) for row in rows]

    def recover_claiming(self, batch_id=None, *, note=""):
        query = (
            "UPDATE credential_inventory SET state='available', claimed_at='', "
            "batch_id='', note=? WHERE state='claiming'"
        )
        params = [str(note)]
        if batch_id is not None:
            query += " AND batch_id=?"
            params.append(str(batch_id))
        with self._connect() as connection:
            cursor = connection.execute(query, params)
            return cursor.rowcount

    def recover_pending(self, *, reason="recovered_pending"):
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, finished_at=?, reason_code=? WHERE status='pending'",
                (JobStatus.CANCELLED.value, datetime.now(timezone.utc).isoformat(), reason),
            )

    def has_imported(self, source_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM jobs WHERE source_fingerprint=? AND status=? LIMIT 1",
                (self.fingerprint(source_id), JobStatus.IMPORTED.value),
            ).fetchone()
        return row is not None

    def imported_fingerprints(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT source_fingerprint FROM jobs WHERE status=?",
                (JobStatus.IMPORTED.value,),
            )
            return {row["source_fingerprint"] for row in rows}

    def aggregate_counts(self):
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(DISTINCT CASE WHEN authorization_started=1 OR status=?
                        THEN source_fingerprint END) AS attempted_unique,
                    COUNT(DISTINCT CASE WHEN status=?
                        THEN source_fingerprint END) AS imported_unique,
                    COUNT(CASE WHEN finished_at IS NOT NULL THEN 1 END) AS finalized_attempts,
                    COUNT(CASE WHEN status=? THEN 1 END) AS imported_attempts,
                    COUNT(CASE WHEN reason_code='rate_limited' THEN 1 END) AS rate_limited
                FROM jobs
                """,
                (
                    JobStatus.IMPORTED.value,
                    JobStatus.IMPORTED.value,
                    JobStatus.IMPORTED.value,
                ),
            ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def get(self, job_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT source_fingerprint, attempt_number, status, started_at, finished_at, "
                "reason_code, sink_receipt_fingerprint FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            return dict(row) if row else None

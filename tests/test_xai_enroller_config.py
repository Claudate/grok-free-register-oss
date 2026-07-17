import os
import sqlite3
import stat

import pytest

from xai_enroller.config import Settings
from xai_enroller.sources import FileSourceAdapter, SQLiteSourceAdapter


def base_env(tmp_path, **overrides):
    values = {
        "XAI_ENROLLER_SOURCE_KIND": "file",
        "XAI_ENROLLER_SOURCE_FILE": str(tmp_path / "sessions.tsv"),
        "XAI_ENROLLER_SOURCE_SALT": "test-salt",
        "XAI_ENROLLER_LEDGER_PATH": str(tmp_path / "ledger.db"),
    }
    values.update(overrides)
    return values


def test_settings_rejects_public_http_cpa_sink(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        Settings.from_environ(
            base_env(
                tmp_path,
                XAI_ENROLLER_SINK="cpa",
                XAI_ENROLLER_CPA_BASE_URL="http://example.test",
                XAI_ENROLLER_CPA_MANAGEMENT_SECRET="secret",
            )
        )


def test_settings_defaults_are_bounded_and_redacted(tmp_path):
    settings = Settings.from_environ(base_env(tmp_path))
    assert settings.target == 1
    assert settings.concurrency == 1
    assert settings.retry_attempts == 0
    assert settings.executor == "http"
    assert settings.poll_interval == 5.0
    redacted = settings.redacted_dict()
    assert "test-salt" not in repr(redacted)
    assert "CPA_MANAGEMENT_SECRET" not in repr(redacted)
    assert "source_salt" not in redacted


def test_settings_accepts_bounded_retry_attempts(tmp_path):
    settings = Settings.from_environ(
        base_env(tmp_path, XAI_ENROLLER_RETRY_ATTEMPTS="2")
    )

    assert settings.retry_attempts == 2


def test_settings_accepts_remote_source_without_a_local_source_file(tmp_path):
    settings = Settings.from_environ(
        base_env(
            tmp_path,
            XAI_ENROLLER_SOURCE_KIND="remote",
            XAI_ENROLLER_SOURCE_FILE="",
        )
    )

    assert settings.source_kind == "remote"
    assert settings.source_file is None
    assert settings.source_db is None


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("XAI_ENROLLER_SOURCE_KIND", "other", "source"),
        ("XAI_ENROLLER_AUTH_EXECUTOR", "other", "executor"),
        ("XAI_ENROLLER_CONCURRENCY", "0", "concurrency"),
        ("XAI_ENROLLER_POLL_SEC", "0", "poll"),
        ("XAI_ENROLLER_RETRY_ATTEMPTS", "-1", "retry"),
        ("XAI_ENROLLER_RETRY_ATTEMPTS", "4", "retry"),
        ("XAI_ENROLLER_TARGET", "0", "target"),
        ("XAI_ENROLLER_TARGET", "101", "target"),
    ],
)
def test_settings_reject_invalid_bounds(tmp_path, key, value, message):
    with pytest.raises(ValueError, match=message):
        Settings.from_environ(base_env(tmp_path, **{key: value}))


def test_file_source_requires_private_regular_file(tmp_path):
    path = tmp_path / "sessions.tsv"
    path.write_text("one\tsecret\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    with pytest.raises(ValueError, match="0600"):
        list(FileSourceAdapter(path).records())


def test_file_source_suppresses_duplicates_without_leaking_tokens(tmp_path):
    path = tmp_path / "sessions.tsv"
    path.write_text("one\tsecret-a\none\tsecret-b\n", encoding="utf-8")
    path.chmod(0o600)
    records = list(FileSourceAdapter(path).records())
    assert len(records) == 1
    assert records[0].source_id == "one"
    assert "secret" not in repr(records)


def test_sqlite_source_uses_fixed_read_only_query_and_hmac_ids(tmp_path):
    path = tmp_path / "accounts.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE accounts (token TEXT, status TEXT, deleted_at TEXT, updated_at INTEGER)"
    )
    connection.executemany(
        "INSERT INTO accounts VALUES (?, ?, ?, ?)",
        [
            ("old", "active", None, 1),
            ("new", "active", None, 2),
            ("deleted", "active", "2026-01-01", 3),
            ("inactive", "inactive", None, 4),
        ],
    )
    connection.commit()
    connection.close()
    records = list(SQLiteSourceAdapter(path, b"salt").records())
    assert [record.sso_token for record in records] == ["new", "old"]
    assert records[0].source_id != records[1].source_id
    assert "new" not in repr(records)

import asyncio
import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from xai_enroller.models import JobStatus
from xai_enroller.service import (
    AuthServiceSettings,
    AuthPipelineRunner,
    EventTerminal,
    InteractiveCommandPrompt,
    resolve_auth_log_mode,
)
from xai_enroller.ledger import Ledger
from xai_enroller.inventory import InventoryError


def test_auth_log_mode_prefers_cli_and_rejects_invalid_values():
    assert resolve_auth_log_mode([], {}) == "user"
    assert resolve_auth_log_mode(
        [], {"XAI_AUTH_SERVICE_LOG_MODE": "debug"}
    ) == "debug"
    assert resolve_auth_log_mode(
        ["--debug"], {"XAI_AUTH_SERVICE_LOG_MODE": "user"}
    ) == "debug"
    with pytest.raises(ValueError, match="XAI_AUTH_SERVICE_LOG_MODE"):
        resolve_auth_log_mode([], {"XAI_AUTH_SERVICE_LOG_MODE": "verbose"})


def test_user_terminal_reports_progress_without_internal_or_secret_fields():
    messages = []
    terminal = EventTerminal(mode="user", output=messages.append)
    terminal.emit(
        (
            "startup",
            {
                "available": 7,
                "claimed": 3,
                "destination": "authenticated/",
                "source_kind": "local",
                "ssh_host": "do-not-print.example",
            },
        )
    )
    terminal.emit(
        (
            "authorization_started",
            {
                "task_number": 3,
                "attempt_number": 1,
                "pending_total": 41,
                "source_queue": 64,
                "source_id": "secret@example.test",
            },
        )
    )
    terminal.emit(
        (
            "result",
            {
                "status": "imported",
                "reason": "imported",
                "task_number": 3,
                "five_minute_imports_per_minute": 4.25,
                "lifetime_imports_per_minute": 1.75,
                "imported_unique": 120,
                "available": 117,
                "source_id": "secret@example.test",
            },
        )
    )

    assert messages == [
        "[✓] 本地认证服务已启动 | 来源 本机 | 输出 authenticated/ | 待处理 — | 可用 7",
        "[→] 开始认证 #3 | 待处理 41",
        "[✓] 认证成功 #3 | 运行平均 1.75/分 | 累计 120 | 可用 117",
    ]
    rendered = "\n".join(messages)
    assert "source_queue" not in rendered
    assert "secret@example.test" not in rendered
    assert "do-not-print.example" not in rendered


def test_user_status_distinguishes_unknown_runtime_rate_from_zero_rate():
    base = {
        "state": "running",
        "pending_total": None,
        "active_stage": "idle",
        "imported_unique": 0,
        "available": 0,
        "claimed": 0,
        "cooldown": False,
    }
    messages = []
    terminal = EventTerminal(mode="user", output=messages.append)
    terminal.emit(("status", {**base, "lifetime_imports_per_minute": None}))
    terminal.emit(("status", {**base, "lifetime_imports_per_minute": 0.0}))

    assert "运行平均 —" in messages[0]
    assert "运行平均 0.00/分" in messages[1]
    assert "source_queue" not in messages[0]


def test_user_terminal_omits_missing_task_number_before_authorization_starts():
    messages = []
    terminal = EventTerminal(mode="user", output=messages.append)

    terminal.emit(
        (
            "result",
            {
                "status": "failed",
                "reason": "device_flow_failed",
                "task_number": None,
            },
        )
    )

    assert messages == ["[✗] 认证未完成 | 暂时失败，将自动重试"]
    assert "None" not in messages[0]


def test_debug_terminal_keeps_aggregate_diagnostics_and_sanitizes_unknown_events():
    messages = []
    terminal = EventTerminal(mode="debug", output=messages.append)
    terminal.emit(
        (
            "status",
            {
                "state": "running",
                "source_queue": 2,
                "prepared_queue": 1,
                "completion_queue": 0,
                "active_stage": "authorization",
                "retry_waiting": 1,
                "next_retry_seconds": 5.0,
                "authorization_starts": 3,
                "cooldown": False,
                "cooldown_remaining_seconds": 0.0,
                "probe_in_flight": False,
                "min_authorization_interval_seconds": 10.0,
                "pacing_remaining_seconds": 2.0,
                "imported_unique": 2,
                "attempted_unique": 3,
                "rate_limited": 1,
                "five_minute_imports_per_minute": 2.0,
                "lifetime_imports_per_minute": 1.0,
                "available": 2,
                "claiming": 0,
                "claimed": 0,
            },
        )
    )
    terminal.emit(
        (
            "future_event",
            {"reason": "internal_error", "token": "do-not-print-token"},
        )
    )

    assert "queues=2/1/0" in messages[0]
    assert messages[1] == "• debug event=future_event reason=internal_error"
    assert "do-not-print-token" not in "\n".join(messages)


def test_terminal_output_failure_does_not_escape():
    def broken_output(_message):
        raise OSError("closed terminal")

    terminal = EventTerminal(mode="user", output=broken_output)
    terminal.emit(("service_stopped", {}))


def test_interactive_prompt_restores_partially_typed_take_command_after_events():
    output = io.StringIO()
    commands = []
    prompt = InteractiveCommandPrompt(
        output=output,
        interactive=True,
        prompt="认证> ",
    )
    prompt.start(commands.append)
    prompt.feed("take 12")

    prompt.write_event("[↻] 发现新账号 3")

    assert output.getvalue().endswith("[↻] 发现新账号 3\n认证> take 12")

    prompt.feed("\r")

    assert commands == ["take 12"]
    assert output.getvalue().endswith("\n认证> ")


def test_auth_service_configuration_errors_are_actionable_without_traceback(tmp_path):
    environment = os.environ.copy()
    environment.pop("XAI_AUTH_SERVICE_SSH_HOST", None)
    environment.pop("XAI_AUTH_SERVICE_LOG_MODE", None)
    environment["XAI_AUTH_SERVICE_SOURCE"] = "ssh"
    missing = subprocess.run(
        [sys.executable, "-m", "xai_enroller.service"],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 2
    assert "XAI_AUTH_SERVICE_SSH_HOST" in missing.stderr
    assert "docs/guides/auth-service.md" in missing.stderr
    assert "Traceback" not in missing.stderr

    environment["XAI_AUTH_SERVICE_LOG_MODE"] = "verbose"
    invalid = subprocess.run(
        [sys.executable, "-m", "xai_enroller.service"],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert invalid.returncode == 2
    assert "XAI_AUTH_SERVICE_LOG_MODE" in invalid.stderr
    assert "Traceback" not in invalid.stderr

    environment["XAI_AUTH_SERVICE_LOG_MODE"] = "user"
    environment.pop("XAI_AUTH_SERVICE_SOURCE", None)
    environment["XAI_AUTH_SERVICE_SSH_HOST"] = "user@example.test"
    environment["XAI_ENROLLER_LOCAL_AUTH_DIR"] = str(tmp_path / "auth")
    environment["XAI_ENROLLER_TIMEOUT_SEC"] = "not-a-number"
    invalid_enroller_setting = subprocess.run(
        [sys.executable, "-m", "xai_enroller.service"],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert invalid_enroller_setting.returncode == 2
    assert "XAI_ENROLLER_TIMEOUT_SEC" in invalid_enroller_setting.stderr
    assert "docs/guides/auth-service.md" in invalid_enroller_setting.stderr
    assert "Traceback" not in invalid_enroller_setting.stderr


def test_auth_service_startup_failure_is_sanitized_without_traceback(tmp_path):
    blocked_destination = tmp_path / "private-output-path"
    blocked_destination.write_text("not a directory", encoding="utf-8")
    environment = os.environ.copy()
    environment["XAI_AUTH_SERVICE_SSH_HOST"] = "user@example.test"
    environment["XAI_ENROLLER_LOCAL_AUTH_DIR"] = str(blocked_destination)

    failed = subprocess.run(
        [sys.executable, "-m", "xai_enroller.service"],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert failed.returncode == 1
    assert "认证服务异常终止" in failed.stderr
    assert "bash auth-service.sh --debug" in failed.stderr
    assert "Traceback" not in failed.stderr
    assert str(blocked_destination) not in failed.stderr


def test_ledger_recognizes_previously_imported_sources(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db", b"test-salt")
    imported = ledger.start("done@example.test")
    ledger.finish(imported, JobStatus.IMPORTED, "imported")
    failed = ledger.start("retry@example.test")
    ledger.finish(failed, JobStatus.SINK_FAILED, "sink_failed")

    assert ledger.has_imported("done@example.test") is True
    assert ledger.has_imported("retry@example.test") is False


def test_auth_service_settings_defaults_local_and_preserves_ssh_auto_detection(tmp_path):
    local = AuthServiceSettings.from_environ(
        {"XAI_AUTH_SERVICE_REGISTER_ROOT": str(tmp_path)}
    )
    remote = AuthServiceSettings.from_environ(
        {
            "XAI_AUTH_SERVICE_SSH_HOST": "ubuntu@example.test",
            "XAI_AUTH_SERVICE_SYNC_SEC": "45",
        }
    )

    assert local.source_kind == "local"
    assert local.ssh_host is None
    assert local.register_root == str(tmp_path)
    assert remote.source_kind == "ssh"
    assert remote.ssh_host == "ubuntu@example.test"
    assert remote.sync_seconds == 45
    assert remote.remote_root == "/opt/grok-free-register"


def test_auth_service_settings_explicit_source_overrides_auto_detection():
    local = AuthServiceSettings.from_environ(
        {
            "XAI_AUTH_SERVICE_SOURCE": "local",
            "XAI_AUTH_SERVICE_SSH_HOST": "ignored@example.test",
        }
    )
    assert local.source_kind == "local"

    with pytest.raises(ValueError, match="XAI_AUTH_SERVICE_SSH_HOST"):
        AuthServiceSettings.from_environ({"XAI_AUTH_SERVICE_SOURCE": "ssh"})
    with pytest.raises(ValueError, match="XAI_AUTH_SERVICE_SOURCE"):
        AuthServiceSettings.from_environ({"XAI_AUTH_SERVICE_SOURCE": "unknown"})


def test_pipeline_runner_takes_a_credential_batch_and_reports_inventory():
    class InventoryLedger:
        def inventory_counts(self):
            return {"available": 7, "claiming": 0, "claimed": 3}

    class Pipeline:
        ledger = InventoryLedger()

        def status(self):
            return {"state": "running"}

    class Inventory:
        def take(self, count):
            assert count == 3
            return SimpleNamespace(
                batch_id="batch-1",
                directory=Path("/tmp/claimed/batch-1"),
                moved=3,
                note="",
            )

    async def scenario():
        events = []
        runner = AuthPipelineRunner(Pipeline(), events.append, inventory=Inventory())
        assert await runner.handle_command("take 3") is True
        assert await runner.handle_command("s") is True
        return events

    assert asyncio.run(scenario()) == [
        (
            "inventory_taken",
            {
                "batch_id": "batch-1",
                "directory": "/tmp/claimed/batch-1",
                "moved": 3,
                "available": 7,
                "claiming": 0,
                "claimed": 3,
            },
        ),
        (
            "status",
            {
                "state": "running",
                "available": 7,
                "claiming": 0,
                "claimed": 3,
            },
        ),
    ]


def test_pipeline_runner_reports_inventory_failure_without_stopping():
    class Ledger:
        def inventory_counts(self):
            return {"available": 1, "claiming": 1, "claimed": 0}

    class Pipeline:
        ledger = Ledger()

    class Inventory:
        def take(self, _count):
            raise InventoryError("credential file is missing")

    async def scenario():
        events = []
        runner = AuthPipelineRunner(Pipeline(), events.append, inventory=Inventory())
        assert await runner.handle_command("take 1") is True
        return events

    assert asyncio.run(scenario()) == [
        (
            "inventory_error",
            {
                "reason": "credential file is missing",
                "available": 1,
                "claiming": 1,
                "claimed": 0,
            },
        )
    ]

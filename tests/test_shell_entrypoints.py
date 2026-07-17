import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _entrypoint_workspace(tmp_path):
    for name in ("start.sh", "auth-service.sh", "setup.sh"):
        shutil.copy2(ROOT / name, tmp_path / name)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts" / "ensure_runtime.sh", scripts / "ensure_runtime.sh")
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$ENTRY_CAPTURE\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    return tmp_path


def _run_entry(workspace, script, *args):
    capture = workspace / "capture.txt"
    environment = {**os.environ, "ENTRY_CAPTURE": str(capture)}
    completed = subprocess.run(
        ["bash", script, *args],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return capture.read_text(encoding="utf-8").splitlines()


def test_start_dispatches_registration_and_email_as_independent_modules(tmp_path):
    workspace = _entrypoint_workspace(tmp_path)
    (workspace / ".env").write_text("EMAIL_MODE=tempmail\n", encoding="utf-8")

    assert _run_entry(workspace, "start.sh", "--target", "3") == [
        "-m",
        "grok_register.register",
        "--target",
        "3",
    ]
    assert _run_entry(workspace, "start.sh", "--email-service", "--port", "9090") == [
        "-m",
        "grok_register.email_server",
        "--port",
        "9090",
    ]


def test_auth_service_keeps_the_supported_python_module_internal(tmp_path):
    workspace = _entrypoint_workspace(tmp_path)
    assert _run_entry(workspace, "auth-service.sh", "--debug") == [
        "-m",
        "xai_enroller.service",
        "--debug",
    ]

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _int(env, key, default):
    try:
        return int(env.get(key, str(default)))
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _float(env, key, default):
    try:
        return float(env.get(key, str(default)))
    except ValueError as exc:
        raise ValueError(f"{key} must be numeric") from exc


@dataclass(frozen=True)
class Settings:
    source_kind: str
    source_file: Path | None
    source_db: Path | None
    source_salt: bytes
    ledger_path: Path
    target: int = 1
    concurrency: int = 1
    retry_attempts: int = 0
    timeout_sec: float = 1800.0
    poll_interval: float = 5.0
    executor: str = "http"
    sink: str | None = None
    cpa_base_url: str | None = None
    cpa_management_secret: str | None = None
    local_auth_dir: Path | None = None

    @classmethod
    def from_environ(cls, env=None):
        env = dict(os.environ if env is None else env)
        source_kind = env.get("XAI_ENROLLER_SOURCE_KIND", "file")
        if source_kind not in {"file", "sqlite", "remote"}:
            raise ValueError("source kind must be file, sqlite, or remote")
        source_file = Path(env["XAI_ENROLLER_SOURCE_FILE"]) if env.get("XAI_ENROLLER_SOURCE_FILE") else None
        source_db = Path(env["XAI_ENROLLER_SOURCE_DB"]) if env.get("XAI_ENROLLER_SOURCE_DB") else None
        if source_kind == "file" and source_file is None:
            raise ValueError("source file is required")
        if source_kind == "sqlite" and source_db is None:
            raise ValueError("source db is required")
        salt = env.get("XAI_ENROLLER_SOURCE_SALT")
        if not salt:
            raise ValueError("source salt is required")
        ledger_path = Path(env.get("XAI_ENROLLER_LEDGER_PATH", "xai-enroller-ledger.db"))
        target = _int(env, "XAI_ENROLLER_TARGET", 1)
        concurrency = _int(env, "XAI_ENROLLER_CONCURRENCY", 1)
        retry_attempts = _int(env, "XAI_ENROLLER_RETRY_ATTEMPTS", 0)
        timeout_sec = _float(env, "XAI_ENROLLER_TIMEOUT_SEC", 1800)
        poll_interval = _float(env, "XAI_ENROLLER_POLL_SEC", 5)
        executor = env.get("XAI_ENROLLER_AUTH_EXECUTOR", "http")
        sink = env.get("XAI_ENROLLER_SINK") or None
        if not 1 <= target <= 100:
            raise ValueError("target must be between 1 and 100")
        if not 1 <= concurrency <= 4:
            raise ValueError("concurrency must be between 1 and 4")
        if not 0 <= retry_attempts <= 3:
            raise ValueError("retry attempts must be between 0 and 3")
        if timeout_sec <= 0:
            raise ValueError("timeout must be positive")
        if poll_interval <= 0:
            raise ValueError("poll interval must be positive")
        if executor not in {"http", "playwright"}:
            raise ValueError("executor must be http or playwright")
        if sink not in {None, "cpa", "local"}:
            raise ValueError("sink must be cpa or local")
        cpa_url = env.get("XAI_ENROLLER_CPA_BASE_URL") or None
        cpa_secret = env.get("XAI_ENROLLER_CPA_MANAGEMENT_SECRET") or None
        local_auth_dir = (
            Path(env["XAI_ENROLLER_LOCAL_AUTH_DIR"]).expanduser()
            if env.get("XAI_ENROLLER_LOCAL_AUTH_DIR")
            else None
        )
        if sink == "cpa":
            if not cpa_url or not cpa_secret:
                raise ValueError("CPA base URL and management secret are required")
            parsed = urlparse(cpa_url)
            is_private = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if parsed.scheme != "https" and not is_private:
                raise ValueError("CPA base URL must use HTTPS")
        if sink == "local" and local_auth_dir is None:
            raise ValueError("local auth directory is required")
        return cls(
            source_kind=source_kind,
            source_file=source_file,
            source_db=source_db,
            source_salt=salt.encode(),
            ledger_path=ledger_path,
            target=target,
            concurrency=concurrency,
            retry_attempts=retry_attempts,
            timeout_sec=timeout_sec,
            poll_interval=poll_interval,
            executor=executor,
            sink=sink,
            cpa_base_url=cpa_url,
            cpa_management_secret=cpa_secret,
            local_auth_dir=local_auth_dir,
        )

    def redacted_dict(self):
        return {
            "source_kind": self.source_kind,
            "source_file": str(self.source_file) if self.source_file else None,
            "source_db": str(self.source_db) if self.source_db else None,
            "ledger_path": str(self.ledger_path),
            "target": self.target,
            "concurrency": self.concurrency,
            "retry_attempts": self.retry_attempts,
            "timeout_sec": self.timeout_sec,
            "poll_interval": self.poll_interval,
            "executor": self.executor,
            "sink": self.sink,
            "cpa_base_url": self.cpa_base_url,
            "local_auth_dir": str(self.local_auth_dir) if self.local_auth_dir else None,
        }


def require_private_regular_file(path: Path, *, require_0600=False):
    try:
        mode = path.stat()
    except OSError as exc:
        raise ValueError(f"source path is unavailable: {path}") from exc
    if not stat.S_ISREG(mode.st_mode):
        raise ValueError("source path must be a regular file")
    if mode.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("source path must not be group/world writable")
    if require_0600 and mode.st_mode & (stat.S_IRGRP | stat.S_IROTH):
        raise ValueError("file source requires mode 0600 or stricter")

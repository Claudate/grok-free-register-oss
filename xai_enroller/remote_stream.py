"""Atomic local snapshots of password-free registration sessions."""

import asyncio
import json
import os
import shlex
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

from .models import SourceRecord

MAX_SESSION_RECORD_BYTES = 256 * 1024


class RemoteStreamError(RuntimeError):
    """A classified stream failure whose text never includes remote output."""


class SSHSnapshotSynchronizer:
    """Atomically refresh one validated, password-free local JSONL snapshot."""

    MAX_STDERR_BYTES = 16 * 1024

    def __init__(
        self,
        host,
        destination,
        *,
        remote_root="/opt/grok-free-register",
        identity_file=None,
        process_factory=asyncio.create_subprocess_exec,
        fingerprint=None,
    ):
        self.host = host
        self.destination = Path(destination)
        self.remote_root = remote_root
        self.identity_file = identity_file
        self.process_factory = process_factory
        self.fingerprint = fingerprint or (lambda source_id: source_id)
        self.snapshot_fingerprints = None
        self._process = None

    def _input_generation(self):
        return None

    def _input_generation_unchanged(self, generation):
        return True

    def _empty_snapshot_allowed(self):
        return False

    def _command(self):
        return (
            f"cd {shlex.quote(self.remote_root)} && "
            "python3 scripts/export_registered_sessions.py "
            "keys/auth-sessions.jsonl keys/accounts.txt"
        )

    def _args(self):
        args = [
            "ssh",
            "-C",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if self.identity_file:
            args.extend(["-i", self.identity_file])
        args.extend(["--", self.host, self._command()])
        return args

    async def _read_stderr(self, stream):
        retained = 0
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            retained = min(self.MAX_STDERR_BYTES, retained + len(chunk))

    async def _terminate(self, process):
        if process is None or process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def close(self):
        await self._terminate(self._process)

    async def sync_once(self):
        self.destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.destination.parent, 0o700)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.destination.name}.",
            suffix=".tmp",
            dir=self.destination.parent,
        )
        process = None
        stderr_task = None
        try:
            os.fchmod(fd, 0o600)
            input_generation = self._input_generation()
            process = await self.process_factory(
                *self._args(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_SESSION_RECORD_BYTES + 1,
            )
            self._process = process
            stderr_task = asyncio.create_task(self._read_stderr(process.stderr))
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                record_count = 0
                snapshot_fingerprints = set()
                while True:
                    try:
                        raw = await process.stdout.readline()
                    except (ValueError, asyncio.LimitOverrunError) as exc:
                        raise ValueError("invalid remote session snapshot") from exc
                    if not raw:
                        break
                    if (
                        not raw.endswith(b"\n")
                        or len(raw) - 1 > MAX_SESSION_RECORD_BYTES
                    ):
                        raise ValueError("invalid remote session snapshot")
                    record = parse_session_document(raw[:-1])
                    snapshot_fingerprints.add(self.fingerprint(record.source_id))
                    stream.write(raw)
                    record_count += 1
                returncode = await process.wait()
                if stderr_task is not None:
                    await stderr_task
                    stderr_task = None
                if returncode != 0:
                    raise RemoteStreamError("remote snapshot export failed")
                if record_count == 0 and not self._empty_snapshot_allowed():
                    raise RemoteStreamError("remote snapshot export was empty")
                if not self._input_generation_unchanged(input_generation):
                    raise RemoteStreamError("snapshot inputs changed during export")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.destination)
            os.chmod(self.destination, 0o600)
            directory_fd = os.open(self.destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self.snapshot_fingerprints = frozenset(snapshot_fingerprints)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        finally:
            if fd >= 0:
                os.close(fd)
            if stderr_task is not None:
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            with suppress(Exception):
                await self._terminate(process)
            if self._process is process:
                self._process = None
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)


class LocalSnapshotSynchronizer(SSHSnapshotSynchronizer):
    """Export registration data from one local project into an atomic snapshot."""

    def __init__(
        self,
        register_root,
        destination,
        *,
        process_factory=asyncio.create_subprocess_exec,
        fingerprint=None,
        python_executable=sys.executable,
        exporter_path=None,
    ):
        super().__init__(
            "",
            destination,
            process_factory=process_factory,
            fingerprint=fingerprint,
        )
        self.register_root = Path(register_root)
        self.python_executable = str(python_executable)
        self.exporter_path = Path(exporter_path or (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "export_registered_sessions.py"
        ))
        self.sessions_path = self.register_root / "keys" / "auth-sessions.jsonl"
        self.accounts_path = self.register_root / "keys" / "accounts.txt"

    @staticmethod
    def _path_generation(path):
        try:
            stat_result = path.stat()
        except FileNotFoundError:
            return None
        return (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
        )

    def _input_generation(self):
        return (
            self._path_generation(self.sessions_path),
            self._path_generation(self.accounts_path),
        )

    def _input_generation_unchanged(self, generation):
        return self._input_generation() == generation

    def _empty_snapshot_allowed(self):
        return True

    def _args(self):
        return [
            self.python_executable,
            str(self.exporter_path),
            str(self.sessions_path),
            str(self.accounts_path),
        ]


class DiskSnapshotSource:
    """Consume immutable local snapshot generations under queue backpressure."""

    def __init__(
        self,
        path,
        *,
        synchronizer=None,
        sync_seconds=30.0,
        poll_seconds=0.25,
        sleep=asyncio.sleep,
        event_callback=None,
        fingerprint=None,
    ):
        self.path = Path(path)
        self.synchronizer = synchronizer
        self.sync_seconds = float(sync_seconds)
        self.poll_seconds = float(poll_seconds)
        self.sleep = sleep
        self.event_callback = event_callback
        self.fingerprint = fingerprint or (lambda source_id: source_id)
        self._snapshot_fingerprints = None
        self._last_reported_snapshot_fingerprints = None
        self._closed = False
        self._sync_task = None
        self._last_sync_ok = None

    def _emit(self, kind, data):
        if self.event_callback is not None:
            with suppress(Exception):
                self.event_callback(kind, data)

    @property
    def snapshot_fingerprints(self):
        if self.synchronizer is not None:
            return self.synchronizer.snapshot_fingerprints
        return self._snapshot_fingerprints

    async def _sync_loop(self):
        while not self._closed:
            refreshed = await self.synchronizer.sync_once()
            current = self.synchronizer.snapshot_fingerprints
            if refreshed != self._last_sync_ok:
                self._emit(
                    "source_connected" if refreshed else "source_disconnected",
                    {} if refreshed else {"reason": "snapshot_sync_failed"},
                )
                self._last_sync_ok = refreshed
            if refreshed and current is not None:
                previous = self._last_reported_snapshot_fingerprints
                if previous is not None:
                    added = current - previous
                    if added:
                        self._emit(
                            "source_updated",
                            {"new": len(added), "total": len(current)},
                        )
                self._last_reported_snapshot_fingerprints = current
            try:
                await asyncio.wait_for(self._wait_closed(), timeout=self.sync_seconds)
            except TimeoutError:
                pass

    async def _wait_closed(self):
        while not self._closed:
            await self.sleep(min(0.25, self.sync_seconds))

    async def close(self):
        self._closed = True
        if self._sync_task is not None:
            self._sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sync_task
            self._sync_task = None
        if self.synchronizer is not None:
            await self.synchronizer.close()

    @staticmethod
    def _generation(stat_result):
        return (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )

    async def records(self):
        if self.synchronizer is not None and self._sync_task is None:
            self._sync_task = asyncio.create_task(self._sync_loop())
        consumed_generation = None
        while not self._closed:
            try:
                current = self.path.stat()
            except FileNotFoundError:
                await self.sleep(self.poll_seconds)
                continue
            generation = self._generation(current)
            if generation == consumed_generation:
                await self.sleep(self.poll_seconds)
                continue
            try:
                stream = self.path.open("rb")
            except FileNotFoundError:
                continue
            with stream:
                opened_generation = self._generation(os.fstat(stream.fileno()))
                generation_fingerprints = set()
                valid_generation = True
                while not self._closed:
                    raw = await asyncio.to_thread(
                        stream.readline, MAX_SESSION_RECORD_BYTES + 2
                    )
                    if not raw:
                        break
                    if (
                        not raw.endswith(b"\n")
                        or len(raw) - 1 > MAX_SESSION_RECORD_BYTES
                    ):
                        self._emit(
                            "source_record_rejected", {"reason": "invalid_record"}
                        )
                        valid_generation = False
                        break
                    try:
                        record = parse_session_document(raw[:-1])
                    except ValueError:
                        self._emit(
                            "source_record_rejected", {"reason": "invalid_record"}
                        )
                        valid_generation = False
                        continue
                    generation_fingerprints.add(self.fingerprint(record.source_id))
                    yield record
            if valid_generation:
                self._snapshot_fingerprints = frozenset(generation_fingerprints)
            consumed_generation = opened_generation


def parse_session_document(raw: bytes | str) -> SourceRecord:
    if len(raw) > MAX_SESSION_RECORD_BYTES:
        raise ValueError("invalid remote session record")
    try:
        document = json.loads(raw)
        source_id = document["email"]
        raw_cookies = document["cookies"]
    except (UnicodeDecodeError, TypeError, ValueError, KeyError) as exc:
        raise ValueError("invalid remote session record") from exc
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("invalid remote session record")
    try:
        source_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid remote session record") from exc
    if not isinstance(raw_cookies, list) or not raw_cookies:
        raise ValueError("invalid remote session record")

    cookies = []
    sso_token = ""
    fallback_sso_token = ""
    allowed = {
        "name",
        "value",
        "url",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
    }
    for raw_cookie in raw_cookies:
        if not isinstance(raw_cookie, dict):
            raise ValueError("invalid remote session record")
        cookie = {key: raw_cookie[key] for key in allowed if key in raw_cookie}
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not name or not isinstance(value, str) or not value:
            raise ValueError("invalid remote session record")
        scope = cookie.get("url") or cookie.get("domain")
        if not isinstance(scope, str) or not scope:
            raise ValueError("invalid remote session record")
        try:
            name.encode("utf-8")
            value.encode("utf-8")
            scope.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("invalid remote session record") from exc
        if name == "sso" and not sso_token:
            sso_token = value
        elif name == "sso-rw" and not fallback_sso_token:
            fallback_sso_token = value
        cookies.append(cookie)
    sso_token = sso_token or fallback_sso_token
    if not sso_token:
        raise ValueError("invalid remote session record")
    return SourceRecord(source_id, sso_token, tuple(cookies))


class RemoteSessionStream:
    """Yield full snapshots and appends from one reconnecting SSH child."""

    MAX_STDERR_BYTES = 16 * 1024
    MAX_RECORD_BYTES = MAX_SESSION_RECORD_BYTES
    RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)

    def __init__(
        self,
        host: str,
        *,
        remote_root: str = "/opt/grok-free-register",
        identity_file: str | None = None,
        process_factory=asyncio.create_subprocess_exec,
        sleep=asyncio.sleep,
        event_callback=None,
    ):
        self.host = host
        self.remote_root = remote_root
        self.identity_file = identity_file
        self.process_factory = process_factory
        self.sleep = sleep
        self.event_callback = event_callback
        self._process = None
        self._closed = False
        self._last_disconnect_reason = None

    def _emit(self, kind, data):
        if self.event_callback is None:
            return
        try:
            self.event_callback(kind, data)
        except Exception:
            pass

    def _command(self):
        return (
            f"cd {shlex.quote(self.remote_root)} && "
            "python3 -u scripts/export_registered_sessions.py --follow "
            "keys/auth-sessions.jsonl keys/accounts.txt"
        )

    def _args(self):
        args = [
            "ssh",
            "-C",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if self.identity_file:
            args.extend(["-i", self.identity_file])
        args.extend(["--", self.host, self._command()])
        return args

    async def _read_stderr(self, stream):
        retained = bytearray()
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            remaining = self.MAX_STDERR_BYTES - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
        return bytes(retained)

    @staticmethod
    def _classify_disconnect(returncode, stderr):
        normalized = stderr.lower()
        if b"permission denied" in normalized or b"host key verification failed" in normalized:
            return "ssh_auth_failed"
        if b"could not resolve" in normalized or b"name or service not known" in normalized:
            return "ssh_resolution_failed"
        if any(
            marker in normalized
            for marker in (b"connection refused", b"connection timed out", b"no route to host")
        ):
            return "ssh_connection_failed"
        if returncode == 3:
            return "remote_snapshot_changed"
        if returncode == 4:
            return "remote_data_invalid"
        return "remote_stream_closed"

    async def _terminate_process(self, process):
        if process is None or process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def close(self):
        self._closed = True
        await self._terminate_process(self._process)

    async def records(self):
        reconnect_index = 0
        while not self._closed:
            process = None
            stderr_task = None
            yielded = False
            reason = "remote_stream_closed"
            try:
                process = await self.process_factory(
                    *self._args(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self.MAX_RECORD_BYTES + 1,
                )
                self._process = process
                stderr_task = asyncio.create_task(self._read_stderr(process.stderr))
                self._emit("source_connected", {})
                while not self._closed:
                    try:
                        raw = await process.stdout.readline()
                    except (ValueError, asyncio.LimitOverrunError):
                        reason = "remote_record_too_large"
                        break
                    if not raw:
                        break
                    if (
                        not raw.endswith(b"\n")
                        or len(raw) - 1 > self.MAX_RECORD_BYTES
                    ):
                        reason = "remote_record_too_large"
                        break
                    try:
                        record = parse_session_document(raw[:-1])
                    except ValueError:
                        self._emit("source_record_rejected", {"reason": "invalid_record"})
                        continue
                    if not yielded:
                        self._last_disconnect_reason = None
                    yielded = True
                    reconnect_index = 0
                    yield record

                # A follow exporter is intentionally long-lived.  Once framing is
                # invalid, waiting for it to exit on its own would hang this source
                # forever, so terminate it before collecting the exit status.
                if reason != "remote_stream_closed":
                    await self._terminate_process(process)
                if process.returncode is None:
                    await process.wait()
                stderr = await stderr_task
                if reason == "remote_stream_closed":
                    reason = self._classify_disconnect(process.returncode, stderr)
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError):
                reason = "ssh_start_failed"
            finally:
                if stderr_task is not None and not stderr_task.done():
                    stderr_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await stderr_task
                await self._terminate_process(process)
                if self._process is process:
                    self._process = None

            if self._closed:
                break
            if reason != self._last_disconnect_reason:
                self._emit("source_disconnected", {"reason": reason})
                self._last_disconnect_reason = reason
            if reason == "remote_snapshot_changed":
                delay = 0.1
            else:
                delay = self.RECONNECT_DELAYS[min(reconnect_index, len(self.RECONNECT_DELAYS) - 1)]
                if not yielded:
                    reconnect_index += 1
            await self.sleep(delay)


PersistentSSHSource = RemoteSessionStream

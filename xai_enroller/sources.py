import base64
import hashlib
import hmac
import sqlite3
from pathlib import Path
from typing import Iterator, Protocol

from .config import require_private_regular_file
from .models import SourceRecord


class SourceAdapter(Protocol):
    def records(self) -> Iterator[SourceRecord]: ...


class FileSourceAdapter:
    def __init__(self, path: Path):
        self.path = Path(path)

    def records(self) -> Iterator[SourceRecord]:
        require_private_regular_file(self.path, require_0600=True)
        seen = set()
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                raw = line.rstrip("\r\n")
                if not raw:
                    continue
                source_id, separator, token = raw.partition("\t")
                if not separator or not source_id or not token:
                    raise ValueError(f"invalid source line {line_number}")
                if source_id in seen:
                    continue
                seen.add(source_id)
                yield SourceRecord(source_id, token)


class SQLiteSourceAdapter:
    def __init__(self, path: Path, salt: bytes):
        self.path = Path(path)
        self.salt = bytes(salt)

    def records(self) -> Iterator[SourceRecord]:
        require_private_regular_file(self.path)
        uri = f"file:{self.path.absolute()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute(
                "SELECT token FROM accounts "
                "WHERE status = 'active' AND deleted_at IS NULL "
                "ORDER BY updated_at DESC"
            )
            seen = set()
            for (token,) in rows:
                fingerprint = hmac.new(self.salt, token.encode(), hashlib.sha256).digest()
                source_id = base64.urlsafe_b64encode(fingerprint).rstrip(b"=").decode()
                if source_id in seen:
                    continue
                seen.add(source_id)
                yield SourceRecord(source_id, token)
        finally:
            connection.close()

"""Rebuild ledger skip-set from on-disk auth JSON that still has email."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def _iter_auth_files(*directories):
    seen = set()
    for directory in directories:
        if directory is None:
            continue
        root = Path(directory).expanduser()
        if not root.is_dir():
            continue
        for path in sorted(root.glob("xai-*.json")):
            key = path.name
            if key in seen:
                continue
            seen.add(key)
            yield path


def _email_from_document(document):
    if not isinstance(document, dict):
        return None
    for key in ("email", "Email", "account"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = document.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get("email")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def reconcile_auth_files_into_ledger(
    ledger,
    *,
    authenticated_dir,
    extra_dirs=(),
    copy_missing_into_authenticated=True,
):
    """Mark sources imported when auth JSON with email is already on disk.

    Returns counts: scanned / with_email / newly_imported / copied.
    """
    authenticated = Path(authenticated_dir).expanduser()
    authenticated.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(authenticated, 0o700)

    scanned = 0
    with_email = 0
    newly_imported = 0
    copied = 0

    for path in _iter_auth_files(authenticated, *extra_dirs):
        scanned += 1
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        email = _email_from_document(document)
        if not email:
            continue
        with_email += 1
        receipt = path.stem  # xai-<digest>
        destination = authenticated / path.name
        if copy_missing_into_authenticated and path.resolve() != destination.resolve():
            if not destination.exists():
                try:
                    shutil.copy2(path, destination)
                    os.chmod(destination, 0o600)
                    copied += 1
                except OSError:
                    pass
        try:
            if ledger.ensure_imported(email, receipt, note=f"reconciled:{path.parent.name}"):
                newly_imported += 1
        except Exception:
            # Never block startup on one bad file.
            continue

    return {
        "scanned": scanned,
        "with_email": with_email,
        "newly_imported": newly_imported,
        "copied": copied,
    }

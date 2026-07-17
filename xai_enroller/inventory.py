import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class InventoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimBatch:
    batch_id: str
    directory: Path
    moved: int
    note: str = ""


class CredentialInventory:
    def __init__(self, ledger, available_directory, claimed_directory):
        self.ledger = ledger
        self.available_directory = Path(available_directory)
        self.claimed_directory = Path(claimed_directory)

    def take(self, limit, note=""):
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("claim count must be an integer") from exc
        if limit <= 0:
            raise ValueError("claim count must be positive")
        note = str(note or "").strip()
        batch_id = self._new_batch_id()
        receipts = self.ledger.claim_available(limit, batch_id)
        return self._complete_batch(batch_id, receipts, note)

    def recover(self):
        rows = self.ledger.pending_claims()
        batches = {}
        for row in rows:
            batches.setdefault(row["batch_id"], []).append(
                row["sink_receipt_fingerprint"]
            )
        recovered = 0
        for batch_id, receipts in batches.items():
            recovered += self._complete_batch(batch_id, receipts, "").moved
        return recovered

    @staticmethod
    def _new_batch_id():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{timestamp}-{secrets.token_hex(3)}"

    @staticmethod
    def _validate_receipt(receipt):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", receipt):
            raise InventoryError("invalid credential receipt")

    @staticmethod
    def _fsync_directory(directory):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _complete_batch(self, batch_id, receipts, note):
        destination_directory = self.claimed_directory / batch_id
        if not receipts:
            return ClaimBatch(batch_id, destination_directory, 0, note)
        self.available_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.claimed_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination_directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        for directory in (
            self.available_directory,
            self.claimed_directory,
            destination_directory,
        ):
            os.chmod(directory, 0o700)

        moved = 0
        for receipt in receipts:
            self._validate_receipt(receipt)
            source = self.available_directory / f"{receipt}.json"
            destination = destination_directory / source.name
            source_exists = source.is_file()
            destination_exists = destination.is_file()
            if source_exists and destination_exists:
                raise InventoryError("credential exists in both inventory locations")
            if not source_exists and not destination_exists:
                raise InventoryError("credential file is missing")
            if source_exists:
                os.replace(source, destination)
                os.chmod(destination, 0o600)
            moved += 1

        self._fsync_directory(self.available_directory)
        self._fsync_directory(destination_directory)
        self._fsync_directory(self.claimed_directory)
        marked = self.ledger.mark_claimed(batch_id, note=note)
        if marked != len(receipts):
            raise InventoryError("inventory batch did not commit completely")
        return ClaimBatch(batch_id, destination_directory, moved, note)

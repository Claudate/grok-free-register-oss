from xai_enroller.inventory import CredentialInventory
from xai_enroller.ledger import Ledger
from xai_enroller.models import JobStatus


def import_credential(ledger, source, receipt):
    job_id = ledger.start(source)
    ledger.finish(job_id, JobStatus.IMPORTED, "imported", receipt)
    return job_id


def test_take_moves_available_credentials_without_changing_imported_jobs(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db", b"salt")
    available = tmp_path / "authenticated"
    claimed = tmp_path / "claimed"
    available.mkdir()
    first_job = import_credential(ledger, "first", "xai-first")
    second_job = import_credential(ledger, "second", "xai-second")
    (available / "xai-first.json").write_text("{}\n", encoding="utf-8")
    (available / "xai-second.json").write_text("{}\n", encoding="utf-8")

    batch = CredentialInventory(ledger, available, claimed).take(2)

    assert batch.moved == 2
    assert batch.note == ""
    assert not list(available.glob("*.json"))
    assert sorted(path.name for path in batch.directory.glob("*.json")) == [
        "xai-first.json",
        "xai-second.json",
    ]
    assert ledger.inventory_counts() == {
        "available": 0,
        "claiming": 0,
        "claimed": 2,
    }
    assert ledger.get(first_job)["status"] == JobStatus.IMPORTED.value
    assert ledger.get(second_job)["status"] == JobStatus.IMPORTED.value


def test_recover_finishes_a_batch_interrupted_before_file_move(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db", b"salt")
    available = tmp_path / "authenticated"
    claimed = tmp_path / "claimed"
    available.mkdir()
    import_credential(ledger, "source", "xai-recover")
    (available / "xai-recover.json").write_text("{}\n", encoding="utf-8")
    assert ledger.claim_available(1, "batch-recover") == ["xai-recover"]

    recovered = CredentialInventory(ledger, available, claimed).recover()

    assert recovered == 1
    assert not (available / "xai-recover.json").exists()
    assert (claimed / "batch-recover" / "xai-recover.json").exists()
    assert ledger.inventory_counts() == {
        "available": 0,
        "claiming": 0,
        "claimed": 1,
    }

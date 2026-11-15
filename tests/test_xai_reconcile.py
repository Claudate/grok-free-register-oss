import json

from xai_enroller.ledger import Ledger
from xai_enroller.reconcile import reconcile_auth_files_into_ledger


def test_reconcile_copies_and_marks_imported(tmp_path):
    authenticated = tmp_path / "authenticated"
    cpa_ready = tmp_path / "cpa_ready"
    cpa_ready.mkdir()
    authenticated.mkdir()

    payload = {
        "type": "xai",
        "email": "resume@example.test",
        "access_token": "a",
        "refresh_token": "r",
    }
    source = cpa_ready / "xai-deadbeefcafebabe.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    ledger = Ledger(tmp_path / "ledger.db", b"salt")
    counts = reconcile_auth_files_into_ledger(
        ledger,
        authenticated_dir=authenticated,
        extra_dirs=(cpa_ready,),
    )
    assert counts["scanned"] == 1
    assert counts["with_email"] == 1
    assert counts["newly_imported"] == 1
    assert counts["copied"] == 1
    assert (authenticated / source.name).exists()
    assert ledger.has_imported("resume@example.test")
    assert ledger.aggregate_counts()["imported_unique"] == 1

    # Second pass is a no-op for imports.
    again = reconcile_auth_files_into_ledger(
        ledger,
        authenticated_dir=authenticated,
        extra_dirs=(cpa_ready,),
    )
    assert again["newly_imported"] == 0

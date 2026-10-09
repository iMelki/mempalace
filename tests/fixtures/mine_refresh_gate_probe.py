"""Deliberate receipt-index break/restored pass through the actual reader."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mempalace.write_receipts import ReceiptStore, sha256_bytes  # noqa: E402
from mempalace.mine_progress import (  # noqa: E402
    MineProgressJournal,
    _digest,
    build_source_manifest,
)
from mempalace.mine_refresh import PrefixRefreshJournal  # noqa: E402


def refresh_probe(root, broken):
    source = root / "source.md"
    source.write_text("old", encoding="utf-8")
    manifest = build_source_manifest(project_path=root, files=[source], contract={})
    progress = MineProgressJournal(root / "progress.jsonl", manifest=manifest)
    progress.append_verified(
        source_index=0,
        source_identity="hmac-sha256:" + "a" * 64,
        receipt={
            "receipt_id": "00000000-0000-4000-8000-000000000001",
            "state": "COMPLETE",
            "disposition": "ZERO_OUTPUT",
        },
        represented_count=0,
    )
    item = dict(manifest["items"][0])
    item["content_hash"] = sha256_bytes(b"new")
    item["item_digest"] = _digest({k: v for k, v in item.items() if k != "item_digest"})
    journal = PrefixRefreshJournal(progress)
    journal.append(0, "prepared", item=item)
    if broken:
        path = next(journal.root.glob("*.json"))
        record = json.loads(path.read_text())
        record["previous_receipt_id"] = "00000000-0000-4000-8000-000000000099"
        record["event_digest"] = _digest({k: v for k, v in record.items() if k != "event_digest"})
        path.write_text(json.dumps(record), encoding="utf-8")
        assert json.loads(path.read_text())["previous_receipt_id"].endswith("99")
    state = PrefixRefreshJournal(progress).state(0)
    assert state["pending"]["item"] == item
    print("PASS: exact refresh predecessor and prepared snapshot read back")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--broken", action="store_true")
    parser.add_argument("--gate", choices=("index", "refresh"), default="index")
    args = parser.parse_args()
    # Retain scratch; do not open a native client, or read any real identity key.
    root = Path(tempfile.mkdtemp(prefix="receipt-gate-"))
    os.environ["HOME"] = str(root)
    os.environ["USERPROFILE"] = str(root)
    if args.gate == "refresh":
        refresh_probe(root, args.broken)
        return
    store = ReceiptStore(root / "fixture-palace")
    run = store.create_run(caller="gate-fixture", mode="test", config={})
    digest = sha256_bytes(b"fixture")
    receipt = store.begin_source(
        run=run,
        source_locator="logical://fixture",
        source_content_hash=digest,
        source_version_hash=digest,
        source_size_bytes=7,
        adapter_name="fixture",
        adapter_version="1",
    )
    receipt.set_expected(drawers=0)
    complete = receipt.complete()
    if args.broken:
        path = next(store.sources_dir.glob("*.json"))
        index = json.loads(path.read_text())
        index["event_path"] = "events/deliberately-missing-complete.json"
        path.write_text(json.dumps(index), encoding="utf-8")
        assert json.loads(path.read_text())["event_path"] == index["event_path"]
    current = store.find_current(complete["source"]["identity"])
    assert current["receipt_id"] == complete["receipt_id"]
    print("PASS: exact indexed terminal receipt read back")


if __name__ == "__main__":
    main()

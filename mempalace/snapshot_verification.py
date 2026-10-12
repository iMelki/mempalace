"""Copy-only SQLite checks; weekly verification never acquires a live lease."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .snapshot_errors import PalaceSnapshotError

WEEKLY_FILENAME = "weekly-integrity-receipt.json"
WEEKLY_SCHEMA = "mempalace-weekly-integrity-receipt/v1"


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sqlite_structure(database):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as reader:
        schema = reader.execute(
            "SELECT type,name,tbl_name,rootpage,sql FROM sqlite_schema ORDER BY type,name"
        ).fetchall()
        return {
            "pageCount": reader.execute("PRAGMA page_count").fetchone()[0],
            "freelistCount": reader.execute("PRAGMA freelist_count").fetchone()[0],
            "schemaHash": "sha256:"
            + hashlib.sha256(
                json.dumps(schema, ensure_ascii=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }


def sqlite_check(database, pragma, *, deadline=None):
    if pragma not in ("quick_check", "integrity_check"):
        raise ValueError("unsupported snapshot check")
    label = pragma.replace("_", " ")
    interrupted_by_deadline = False

    def interrupt_on_deadline():
        nonlocal interrupted_by_deadline
        interrupted_by_deadline = time.monotonic() >= deadline
        return int(interrupted_by_deadline)

    try:
        with closing(
            sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as reader:
            if deadline is not None:
                reader.set_progress_handler(interrupt_on_deadline, 10000)
            result = reader.execute("PRAGMA " + pragma).fetchall()
    except sqlite3.Error as exc:
        # Error codes are available on Python 3.11+. Older runtimes also require
        # our callback's witness and SQLite's exact interruption diagnostic.
        code = getattr(exc, "sqlite_errorcode", None)
        interrupted = (code is not None and code & 0xFF == 9) or (
            code is None and str(exc) == "interrupted"
        )
        if interrupted_by_deadline and interrupted:
            raise PalaceSnapshotError("weekly verification budget exceeded") from exc
        raise PalaceSnapshotError(f"snapshot SQLite {label} failed: {exc}") from exc
    if result != [("ok",)]:
        raise PalaceSnapshotError(f"snapshot SQLite {label} did not return ok: {result!r}")
    check_deadline(deadline)
    return "ok"


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise PalaceSnapshotError("weekly verification budget exceeded")


def bounded_hash(path, deadline):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            check_deadline(deadline)
            block = stream.read(8 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def no_links(path, boundary):
    """Reject redirects below the writer-accepted backup root, before resolving."""
    from .backup_snapshot import _is_link_or_reparse

    if boundary not in path.parents:
        raise PalaceSnapshotError("weekly verification path is outside snapshot tree")
    for ancestor in (path, *path.parents):
        if ancestor == boundary:
            break
        if _is_link_or_reparse(ancestor):
            raise PalaceSnapshotError("weekly verification refuses links or reparse points")


def latest_snapshot(root):
    from .backup_snapshot import SNAPSHOT_RECEIPT_FILENAME, SNAPSHOT_RECEIPT_SCHEMA

    root = root.resolve(strict=True)
    candidates = []
    for generation in root.iterdir():
        path = generation / SNAPSHOT_RECEIPT_FILENAME
        if not generation.is_dir() or not path.is_file():
            continue
        no_links(path, root)
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            raise PalaceSnapshotError("snapshot receipt is not an object")
        if receipt.get("schema") != SNAPSHOT_RECEIPT_SCHEMA:
            raise PalaceSnapshotError("unexpected snapshot receipt schema")
        if receipt.get("status") == "complete":
            generated = datetime.fromisoformat(receipt["generatedAt"].replace("Z", "+00:00"))
            if generated.tzinfo is None:
                raise PalaceSnapshotError("snapshot receipt timestamp must include timezone")
            candidates.append((generated, generation.name, path, receipt))
    if not candidates:
        raise PalaceSnapshotError("no complete snapshot copy available for weekly verification")
    _, _, path, receipt = max(candidates, key=lambda item: item[:2])
    return path, receipt


def checked_copy(receipt_path, receipt, live):
    database = receipt_path.parent / "palace" / "chroma.sqlite3"
    boundary = receipt_path.parent.parent
    no_links(database, boundary)
    # Check every segment/metadata entry too; do not follow a redirect before
    # examining it. os.walk yields child directories before descending into them.
    for directory, folders, files in os.walk(receipt_path.parent, followlinks=False):
        for name in folders + files:
            no_links(Path(directory) / name, boundary)
    if Path(receipt.get("stagedRoot", "")).resolve() != database.parent.resolve():
        raise PalaceSnapshotError("weekly staged root does not match snapshot generation")
    database = database.resolve(strict=True)
    for live_root in (live, Path(receipt["palaceRoot"]).resolve()):
        live_database = live_root / "chroma.sqlite3"
        if live_root == database.parent or live_root in database.parents:
            raise PalaceSnapshotError("weekly verification refuses live database")
        if live_database.exists() and os.path.samefile(database, live_database):
            raise PalaceSnapshotError("weekly verification refuses live database hard link")
    return database


def full_copy_proof(database, path, snapshot, result, deadline):
    from .backup_snapshot import verify_snapshot_receipt

    validation = verify_snapshot_receipt(path, verify_hashes=False)
    if not validation["valid"]:
        raise PalaceSnapshotError(
            "weekly snapshot receipt invalid: " + ",".join(validation["problems"])
        )
    expected = next(
        entry["sha256"]
        for entry in snapshot["contentIdentity"]["files"]
        if entry["relativePath"] == "chroma.sqlite3"
    )
    before = bounded_hash(database, deadline)
    if before != expected:
        raise PalaceSnapshotError("weekly SQLite snapshot hash mismatch")
    result["databaseSha256"] = before
    result["integrityCheck"] = sqlite_check(database, "integrity_check", deadline=deadline)
    if bounded_hash(database, deadline) != before:
        raise PalaceSnapshotError("weekly SQLite copy changed during verification")
    if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != result["snapshotReceiptSha256"]:
        raise PalaceSnapshotError("weekly snapshot receipt changed during verification")
    check_deadline(deadline)


def write_weekly_receipt(target, result):
    no_links(target, target.parent.parent)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    temporary.replace(target)


def verify_weekly_snapshot(backup_root, *, palace_path, budget_seconds=7200):
    """Check the latest complete COPY with its own deadline and identity receipt.

    The progress handler bounds SQLite CPU work; chunked hashing shares the
    same deadline. An external job owner must bound blocked OS I/O and process
    lifetime (proposed outer budget 7500s). No live connection is ever opened.
    """
    if not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise PalaceSnapshotError("weekly budget must be finite and positive")
    started = time.monotonic()
    deadline = started + budget_seconds
    path, snapshot = latest_snapshot(Path(backup_root).expanduser().absolute())
    database = checked_copy(path, snapshot, Path(palace_path).expanduser().resolve())
    target = path.parent / WEEKLY_FILENAME
    no_links(target, path.parent.parent)
    result = {
        "schema": WEEKLY_SCHEMA,
        "status": "error",
        "generatedAt": utc_now(),
        "snapshotReceiptSha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "snapshotGeneratedAt": snapshot["generatedAt"],
        "budgetSeconds": budget_seconds,
        "sqliteVersion": sqlite3.sqlite_version,
        "integrityCheck": "not-completed",
        "caller": "mempalace-weekly-integrity",
        "mutationIntent": "weekly-sidecar-only",
        "inputBytes": database.stat().st_size,
    }
    try:
        full_copy_proof(database, path, snapshot, result, deadline)
        result["status"] = "complete"
    except (PalaceSnapshotError, OSError, ValueError, KeyError, StopIteration) as exc:
        result["message"] = str(exc) or "weekly SQLite identity is missing"
    result["durationSeconds"] = round(time.monotonic() - started, 3)
    result["processedCount"] = int(result["status"] == "complete")
    result["errorCount"] = int(result["status"] != "complete")
    result["finishedAt"] = utc_now()
    write_weekly_receipt(target, result)
    return result


def weekly_status(receipt_path, snapshot):
    target = receipt_path.parent / WEEKLY_FILENAME
    try:
        no_links(target, receipt_path.parent.parent)
        if not target.exists():
            return {"status": "missing", "problem": "weekly-integrity-receipt-missing"}
        weekly = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(weekly, dict):
            raise ValueError("weekly receipt is not an object")
        if weekly.get("schema") != WEEKLY_SCHEMA:
            raise ValueError("unexpected weekly schema")
        expected = "sha256:" + hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        if weekly.get("snapshotReceiptSha256") != expected:
            raise ValueError("weekly snapshot receipt identity mismatch")
        entries = snapshot.get("contentIdentity", {}).get("files", [])
        database_hash = next(e["sha256"] for e in entries if e["relativePath"] == "chroma.sqlite3")
        if weekly.get("status") != "complete" or weekly.get("integrityCheck") != "ok":
            return {"status": "failed", "problem": weekly.get("message", "full check incomplete")}
        if weekly.get("databaseSha256") != database_hash:
            raise ValueError("weekly database identity mismatch")
        age = datetime.now(timezone.utc) - datetime.fromisoformat(
            weekly["finishedAt"].replace("Z", "+00:00")
        )
        if age.total_seconds() < 0 or age.total_seconds() > 7 * 86400:
            return {"status": "stale", "problem": "weekly-integrity-receipt-outside-seven-days"}
        return {"status": "complete", "finishedAt": weekly["finishedAt"]}
    except (OSError, ValueError, KeyError, TypeError, StopIteration, PalaceSnapshotError) as exc:
        return {"status": "invalid", "problem": str(exc)}


def receipt_verification_exit_code(payload):
    """0=complete proof, 2=failed proof, 3=missing/stale weekly warning."""
    if not payload.get("valid"):
        return 2
    status = payload.get("weeklyVerification", {}).get("status")
    if status == "complete":
        return 0
    if status in ("missing", "stale"):
        return 3
    return 2

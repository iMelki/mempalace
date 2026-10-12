"""Copy-only SQLite checks; weekly verification never acquires a live lease."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .snapshot_errors import PalaceSnapshotError

WEEKLY_FILENAME = "weekly-integrity-receipt.json"
WEEKLY_SCHEMA = "mempalace-weekly-integrity-receipt/v1"
WEEKLY_INVALIDATION_FILENAME = "weekly-integrity-invalidation.json"


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
        try:
            metadata = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
            relative = ancestor.relative_to(boundary).as_posix()
            raise PalaceSnapshotError(f"snapshot verification refuses file hard link: {relative}")


def isolated_tree(tree, boundary):
    """Guard every entry before inventory, hashing or SQLite reads in any verifier."""
    no_links(tree, boundary)

    def walk_error(exc):
        raise exc

    for directory, folders, files in os.walk(tree, followlinks=False, onerror=walk_error):
        for name in folders + files:
            no_links(Path(directory) / name, boundary)


def outside_live(root, live):
    """A resolved backup boundary may never be equal to or below live storage."""
    if root == live or live in root.parents:
        raise PalaceSnapshotError("backup root is inside live palace storage")


def read_generation(path, root):
    from .backup_snapshot import SNAPSHOT_RECEIPT_SCHEMA

    no_links(path, root)
    if not path.parent.is_dir() or not path.is_file():
        return None
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise PalaceSnapshotError("snapshot receipt is not an object")
    if receipt.get("schema") != SNAPSHOT_RECEIPT_SCHEMA:
        raise PalaceSnapshotError("unexpected snapshot receipt schema")
    if receipt.get("status") != "complete":
        return None
    for field in ("generatedAt", "palaceRoot", "stagedRoot"):
        if not isinstance(receipt.get(field), str) or not receipt[field]:
            raise PalaceSnapshotError(f"snapshot receipt {field} must be a nonempty string")
    generated = datetime.fromisoformat(receipt["generatedAt"].replace("Z", "+00:00"))
    if generated.tzinfo is None:
        raise PalaceSnapshotError("snapshot receipt timestamp must include timezone")
    return generated, path.parent.name, path, receipt


def latest_snapshot(root, *, scan_errors=None, deadline=None):
    """Select a complete copy; one broken sibling cannot abort the scan."""
    from .backup_snapshot import SNAPSHOT_RECEIPT_FILENAME

    root = root.resolve(strict=True)
    candidates = []
    for generation in root.iterdir():
        check_deadline(deadline)
        try:
            candidate = read_generation(generation / SNAPSHOT_RECEIPT_FILENAME, root)
        except Exception as exc:
            # A sibling is untrusted input. JSON recursion and unexpected parser
            # failures must not hide a valid copy or suppress a scan receipt.
            if scan_errors is not None:
                scan_errors.append({"generation": generation.name, "message": str(exc)})
            continue
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        raise PalaceSnapshotError("no complete snapshot copy available for weekly verification")
    _, _, path, receipt = max(candidates, key=lambda item: item[:2])
    return path, receipt


def checked_copy(receipt_path, receipt, live):
    database = receipt_path.parent / "palace" / "chroma.sqlite3"
    boundary = receipt_path.parent.parent
    no_links(database, boundary)

    isolated_tree(receipt_path.parent, boundary)
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


def write_snapshot_json(target, result):
    """Publish JSON by exclusive temporary file and replace; never truncate a link."""
    no_links(target, target.parent.parent)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=True, indent=2, sort_keys=True)
        stream.write("\n")
    temporary.replace(target)


def write_weekly_receipt(target, result):
    write_snapshot_json(target, result)


def save_weekly_result(target, root, result):
    """An unsafe/unwritable generation must not suppress the safe root error."""
    fallback = root / WEEKLY_FILENAME
    result["receiptLocation"] = "backup-root" if target == fallback else "generation"
    try:
        write_weekly_receipt(target, result)
    except (PalaceSnapshotError, OSError) as exc:
        if target == fallback:
            raise
        result["status"] = "error"
        result.setdefault("message", str(exc))
        result["receiptWriteError"] = str(exc)
        result["processedCount"] = 0
        result["errorCount"] = 1
        result["receiptLocation"] = "backup-root"
        # Keep negative authority beside its generation so a later root fallback
        # for another copy cannot resurrect this copy's old success.
        invalidation = target.with_name(WEEKLY_INVALIDATION_FILENAME)
        try:
            write_snapshot_json(invalidation, result)
        except (PalaceSnapshotError, OSError) as invalidation_error:
            result["invalidationWriteError"] = str(invalidation_error)
        write_weekly_receipt(fallback, result)


def weekly_result(budget_seconds):
    """Unknown identities and sizes remain null until a safe copy is selected."""
    return {
        "schema": WEEKLY_SCHEMA,
        "status": "error",
        "generatedAt": utc_now(),
        "snapshotReceiptSha256": None,
        "snapshotGeneratedAt": None,
        "budgetSeconds": budget_seconds,
        "sqliteVersion": sqlite3.sqlite_version,
        "integrityCheck": "not-completed",
        "caller": "mempalace-weekly-integrity",
        "mutationIntent": "weekly-sidecar-only",
        "inputBytes": None,
        "scanErrors": [],
    }


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
    # The configured root is a writer-accepted boundary. Never use a rejected
    # generation as the fallback destination for a scan failure.
    root = Path(backup_root).expanduser().resolve(strict=True)
    live = Path(palace_path).expanduser().resolve()
    outside_live(root, live)
    target = root / WEEKLY_FILENAME
    result = weekly_result(budget_seconds)
    recorded_live = None
    try:
        path, snapshot = latest_snapshot(root, scan_errors=result["scanErrors"], deadline=deadline)
        recorded_live = Path(snapshot["palaceRoot"]).expanduser().resolve()
        outside_live(root, recorded_live)
        target = path.parent / WEEKLY_FILENAME
        no_links(target, root)
        result["snapshotGeneratedAt"] = snapshot["generatedAt"]
        result["snapshotReceiptSha256"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        database = checked_copy(path, snapshot, live)
        result["inputBytes"] = database.stat().st_size
        full_copy_proof(database, path, snapshot, result, deadline)
        result["status"] = "complete"
    except (PalaceSnapshotError, OSError, ValueError, KeyError, StopIteration) as exc:
        result["message"] = str(exc) or "weekly SQLite identity is missing"
    result["durationSeconds"] = round(time.monotonic() - started, 3)
    result["processedCount"] = int(result["status"] == "complete")
    result["errorCount"] = int(result["status"] != "complete")
    result["scanErrorCount"] = len(result["scanErrors"])
    result["finishedAt"] = utc_now()
    if recorded_live is not None:
        outside_live(root, recorded_live)
    save_weekly_result(target, root, result)
    return result


def newer_weekly_failure(receipt_path, expected, weekly):
    """A saved failure for this receipt supersedes older complete proof."""
    root = receipt_path.parent.parent
    targets = (receipt_path.parent / WEEKLY_INVALIDATION_FILENAME, root / WEEKLY_FILENAME)
    for target in targets:
        no_links(target, root)
        if not target.exists():
            continue
        failure = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(failure, dict) or failure.get("schema") != WEEKLY_SCHEMA:
            raise ValueError("unexpected weekly failure schema")
        if failure.get("snapshotReceiptSha256") != expected or failure.get("status") != "error":
            continue
        failed_at = datetime.fromisoformat(failure["generatedAt"].replace("Z", "+00:00"))
        checked_at = datetime.fromisoformat(weekly["generatedAt"].replace("Z", "+00:00"))
        if failed_at >= checked_at:
            return {"status": "failed", "problem": failure.get("message", "receipt write failed")}
    return None


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
        failure = newer_weekly_failure(receipt_path, expected, weekly)
        if failure is not None:
            return failure
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
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        StopIteration,
        RecursionError,
        PalaceSnapshotError,
    ) as exc:
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

"""Disposable process-restart proof for managed MemPalace rewrites.

The probe never opens a configured palace. It creates a synthetic Chroma database,
publishes an exact rewrite recovery snapshot, hard-exits after a partial replacement,
and requires fresh processes to restore and query the original row.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import chromadb
from chromadb.config import Settings

from .palace import mine_palace_lock
from .receipt_verifier import verify_receipt
from .write_receipts import (
    ReceiptRecoveryError,
    ReceiptStore,
    managed_write_scope,
    purge_managed_source_snapshot,
    sha256_bytes,
    snapshot_managed_source_rows,
    write_receipted_collection_batch,
)

SCHEMA = "mempalace-write-receipt-restart-probe/v1"
PHASE_SCHEMA = "mempalace-write-receipt-restart-probe-phase/v1"
EXPECTED_INTERRUPT_EXIT = 73
COLLECTION_NAME = "drawers"
SOURCE_LOCATOR = "logical://mempalace/restart-probe/source"
BASELINE_ROW_ID = "restart-probe-baseline"
PARTIAL_ROW_ID = "restart-probe-partial"
BASELINE_DOCUMENT = "synthetic durable baseline for process restart proof"
PARTIAL_DOCUMENT = "synthetic interrupted replacement for process restart proof"
BASELINE_EMBEDDING = [1.0, 0.0, 0.0, 0.0]
PARTIAL_EMBEDDING = [0.0, 1.0, 0.0, 0.0]
HNSW_METADATA = {
    "hnsw:space": "cosine",
    "hnsw:num_threads": 1,
    "hnsw:batch_size": 50_000,
    "hnsw:sync_threshold": 50_000,
}
WORKSPACE_MARKER = ".mempalace-restart-probe-workspace.json"
WORKSPACE_MARKER_SCHEMA = "mempalace-write-receipt-restart-probe-workspace/v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _durable_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _new_client(palace_path: Path):
    palace_path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(palace_path),
        settings=Settings(anonymized_telemetry=False),
    )


def _collection(client: Any, *, create: bool):
    if create:
        return client.get_or_create_collection(
            COLLECTION_NAME,
            metadata=HNSW_METADATA,
            embedding_function=None,
        )
    return client.get_collection(COLLECTION_NAME, embedding_function=None)


def _close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if not callable(close):
        raise RuntimeError("installed Chroma client has no supported close() boundary")
    close()


def _row(collection: Any, item_id: str) -> Optional[dict[str, Any]]:
    result = collection.get(
        ids=[item_id],
        include=["documents", "metadatas", "embeddings"],
    )
    ids = list(result.get("ids") or [])
    if not ids:
        return None
    embeddings = result.get("embeddings")
    embedding = embeddings[0] if embeddings is not None and len(embeddings) else None
    if hasattr(embedding, "tolist"):
        embedding = embedding.tolist()
    return {
        "id": ids[0],
        "document": list(result.get("documents") or [None])[0],
        "metadata": list(result.get("metadatas") or [None])[0],
        "embedding": embedding,
    }


def _embedding_matches(actual: Any, expected: Sequence[float]) -> bool:
    if actual is None or len(actual) != len(expected):
        return False
    return all(abs(float(left) - float(right)) <= 1e-6 for left, right in zip(actual, expected))


def _phase_path(artifact_dir: Path, phase: str) -> Path:
    return artifact_dir / f"phase-{phase}.json"


def _validate_phase_workspace(workspace: Path, phase_token: str, *, seed: bool) -> None:
    resolved = workspace.resolve(strict=True)
    if workspace.is_symlink():
        raise RuntimeError("disposable probe workspace must not be a symlink")
    marker = _read_json(resolved / WORKSPACE_MARKER)
    if (
        marker.get("schema") != WORKSPACE_MARKER_SCHEMA
        or marker.get("disposable") is not True
        or not secrets.compare_digest(str(marker.get("phaseToken", "")), phase_token)
    ):
        raise RuntimeError("disposable probe workspace marker is missing or invalid")
    palace_path = (resolved / "palace").resolve()
    if os.path.commonpath((str(resolved), str(palace_path))) != str(resolved):
        raise RuntimeError("probe palace escaped its disposable workspace")
    if seed and palace_path.exists():
        raise RuntimeError("seed phase refuses a pre-existing palace directory")
    if not seed and not palace_path.is_dir():
        raise RuntimeError("restart phase requires the disposable seeded palace")


def _phase_seed(workspace: Path, artifact_dir: Path) -> None:
    palace_path = workspace / "palace"
    client = _new_client(palace_path)
    try:
        collection = _collection(client, create=True)
        store = ReceiptStore(palace_path)
        content = BASELINE_DOCUMENT.encode("utf-8")
        content_hash = sha256_bytes(content)
        run = store.create_run(
            caller="mempalace-restart-probe",
            mode="disposable-seed",
            config={"schema": SCHEMA, "phase": "seed"},
        )
        session = store.begin_source(
            run=run,
            source_locator=SOURCE_LOCATOR,
            source_content_hash=content_hash,
            source_version_hash=content_hash,
            source_size_bytes=len(content),
            adapter_name="restart-probe",
            adapter_version="1",
            local_path=False,
        )
        session.set_expected(drawers=1)
        session.running("writing-baseline")
        with managed_write_scope(store.palace_path, lock_factory=mine_palace_lock):
            write_receipted_collection_batch(
                collection,
                "upsert",
                {
                    "ids": [BASELINE_ROW_ID],
                    "documents": [BASELINE_DOCUMENT],
                    "metadatas": [{"probe": True}],
                    "embeddings": [BASELINE_EMBEDDING],
                },
                session=session,
                source_file=SOURCE_LOCATOR,
                collection_name=COLLECTION_NAME,
                local_path=False,
            )
            receipt = session.complete()
        verification = verify_receipt(receipt, collection, store=store)
        row = _row(collection, BASELINE_ROW_ID)
        if verification.status != "represented" or row is None:
            raise RuntimeError("baseline receipt is not represented before clean close")
        if not _embedding_matches(row["embedding"], BASELINE_EMBEDDING):
            raise RuntimeError("baseline embedding readback does not match")
        payload = {
            "schema": PHASE_SCHEMA,
            "phase": "seed",
            "status": "ok",
            "finishedAt": _utc_now(),
            "receiptId": receipt["receipt_id"],
            "sourceIdentity": receipt["source"]["identity"],
            "sourceContentHash": content_hash,
            "verification": verification.as_dict(include_identities=False),
            "rowId": BASELINE_ROW_ID,
            "embeddingDimension": len(BASELINE_EMBEDDING),
        }
        _durable_json(_phase_path(artifact_dir, "seed"), payload)
    finally:
        _close_client(client)


def _load_seed(artifact_dir: Path) -> dict[str, Any]:
    seed = _read_json(_phase_path(artifact_dir, "seed"))
    if seed.get("schema") != PHASE_SCHEMA or seed.get("status") != "ok":
        raise RuntimeError("seed phase evidence is missing or invalid")
    return seed


def _phase_interrupt(workspace: Path, artifact_dir: Path) -> None:
    seed = _load_seed(artifact_dir)
    palace_path = workspace / "palace"
    client = _new_client(palace_path)
    collection = _collection(client, create=False)
    store = ReceiptStore(palace_path)
    source_identity = seed["sourceIdentity"]
    baseline = store.find_current(source_identity)
    if baseline is None or baseline.get("receipt_id") != seed["receiptId"]:
        raise RuntimeError("fresh process did not reopen the authoritative baseline receipt")
    if verify_receipt(baseline, collection, store=store).status != "represented":
        raise RuntimeError("fresh process did not reopen a represented baseline row")

    replacement = PARTIAL_DOCUMENT.encode("utf-8")
    replacement_hash = sha256_bytes(replacement)
    run = store.create_run(
        caller="mempalace-restart-probe",
        mode="disposable-hard-exit",
        config={"schema": SCHEMA, "phase": "interrupt"},
    )
    session = store.begin_source(
        run=run,
        source_locator=SOURCE_LOCATOR,
        source_content_hash=replacement_hash,
        source_version_hash=replacement_hash,
        source_size_bytes=len(replacement),
        adapter_name="restart-probe",
        adapter_version="1",
        local_path=False,
    )
    session.supersede(baseline, reason="disposable-process-restart-proof")
    session.set_expected(drawers=1)

    with managed_write_scope(store.palace_path, lock_factory=mine_palace_lock):
        snapshot = snapshot_managed_source_rows(
            collection,
            source_file=SOURCE_LOCATOR,
            source_identity=source_identity,
            local_path=False,
        )
        if snapshot.ids != (BASELINE_ROW_ID,) or snapshot.embeddings is None:
            raise ReceiptRecoveryError("baseline snapshot is incomplete")
        recovery_path = store.prepare_rewrite_recovery(
            session=session,
            snapshots={COLLECTION_NAME: snapshot},
            source_file=SOURCE_LOCATOR,
            local_path=False,
            previous_receipt=baseline,
        )
        session.running("recovery-published")
        purge_managed_source_snapshot(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name=COLLECTION_NAME,
            source_file=SOURCE_LOCATOR,
            source_identity=source_identity,
            local_path=False,
        )
        write_receipted_collection_batch(
            collection,
            "upsert",
            {
                "ids": [PARTIAL_ROW_ID],
                "documents": [PARTIAL_DOCUMENT],
                "metadatas": [{"probe": True, "interrupted": True}],
                "embeddings": [PARTIAL_EMBEDDING],
            },
            session=session,
            source_file=SOURCE_LOCATOR,
            collection_name=COLLECTION_NAME,
            local_path=False,
        )
        if (
            _row(collection, BASELINE_ROW_ID) is not None
            or _row(collection, PARTIAL_ROW_ID) is None
        ):
            raise RuntimeError("partial rewrite state was not observed before hard exit")
        _durable_json(
            _phase_path(artifact_dir, "interrupt"),
            {
                "schema": PHASE_SCHEMA,
                "phase": "interrupt",
                "status": "expected-hard-exit-ready",
                "finishedAt": _utc_now(),
                "receiptId": session.receipt_id,
                "sourceIdentity": source_identity,
                "recoveryPublished": True,
                "baselineAbsent": True,
                "partialPresent": True,
                "clientCloseCalled": False,
                "expectedExitCode": EXPECTED_INTERRUPT_EXIT,
            },
        )
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(EXPECTED_INTERRUPT_EXIT)


def _phase_recover(workspace: Path, artifact_dir: Path) -> None:
    seed = _load_seed(artifact_dir)
    interrupt = _read_json(_phase_path(artifact_dir, "interrupt"))
    if interrupt.get("status") != "expected-hard-exit-ready":
        raise RuntimeError("durable partial-rewrite marker is missing")
    palace_path = workspace / "palace"
    client = _new_client(palace_path)
    try:
        collection = _collection(client, create=False)
        store = ReceiptStore(palace_path)
        with managed_write_scope(store.palace_path, lock_factory=mine_palace_lock):
            outcomes = store.reconcile_pending_rewrites(
                {COLLECTION_NAME: collection},
                source_identity=seed["sourceIdentity"],
            )
        expected = (
            {
                "receipt_id": interrupt["receiptId"],
                "source_identity": seed["sourceIdentity"],
                "action": "restore",
            },
        )
        if outcomes != expected:
            raise RuntimeError(f"unexpected recovery outcome: {outcomes!r}")
        baseline = store.find_current(seed["sourceIdentity"])
        if baseline is None or baseline.get("receipt_id") != seed["receiptId"]:
            raise RuntimeError("recovery did not preserve the authoritative baseline")
        verification = verify_receipt(baseline, collection, store=store)
        if verification.status != "represented":
            raise RuntimeError("restored baseline is not represented")
        row = _row(collection, BASELINE_ROW_ID)
        if row is None or not _embedding_matches(row["embedding"], BASELINE_EMBEDDING):
            raise RuntimeError("restored baseline embedding does not match")
        if _row(collection, PARTIAL_ROW_ID) is not None:
            raise RuntimeError("interrupted replacement survived recovery")
        _durable_json(
            _phase_path(artifact_dir, "recover"),
            {
                "schema": PHASE_SCHEMA,
                "phase": "recover",
                "status": "ok",
                "finishedAt": _utc_now(),
                "action": "restore",
                "restoredRows": 1,
                "partialRowsRemoved": 1,
                "verification": verification.as_dict(include_identities=False),
            },
        )
    finally:
        _close_client(client)


def _sqlite_integrity(palace_path: Path) -> str:
    database = palace_path / "chroma.sqlite3"
    if not database.is_file():
        raise RuntimeError("Chroma SQLite database is missing")
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    values = [str(row[0]) for row in rows]
    if values != ["ok"]:
        raise RuntimeError(f"SQLite integrity check failed: {values!r}")
    return values[0]


def _phase_verify(workspace: Path, artifact_dir: Path) -> None:
    seed = _load_seed(artifact_dir)
    palace_path = workspace / "palace"
    client = _new_client(palace_path)
    try:
        collection = _collection(client, create=False)
        store = ReceiptStore(palace_path)
        current = store.find_current(seed["sourceIdentity"])
        if current is None or current.get("receipt_id") != seed["receiptId"]:
            raise RuntimeError("second fresh process did not preserve the baseline lineage")
        verification = verify_receipt(current, collection, store=store)
        row = _row(collection, BASELINE_ROW_ID)
        if verification.status != "represented" or row is None:
            raise RuntimeError("second fresh process did not reopen the restored baseline")
        if not _embedding_matches(row["embedding"], BASELINE_EMBEDDING):
            raise RuntimeError("second fresh process reopened a different embedding")
        if _row(collection, PARTIAL_ROW_ID) is not None:
            raise RuntimeError("partial row reappeared after another process restart")
        query = collection.query(
            query_embeddings=[BASELINE_EMBEDDING],
            n_results=1,
            include=["documents", "metadatas", "distances"],
        )
        query_ids = list((query.get("ids") or [[]])[0])
        if query_ids != [BASELINE_ROW_ID]:
            raise RuntimeError(f"restored vector query returned unexpected ids: {query_ids!r}")
        recoveries = sorted(store.recoveries_dir.glob("*/*.json"))
        if recoveries:
            raise RuntimeError("recovery manifests remain after successful restoration")
        integrity = _sqlite_integrity(palace_path)
        _durable_json(
            _phase_path(artifact_dir, "verify"),
            {
                "schema": PHASE_SCHEMA,
                "phase": "verify",
                "status": "ok",
                "finishedAt": _utc_now(),
                "verification": verification.as_dict(include_identities=False),
                "vectorQueryTopId": query_ids[0],
                "sqliteIntegrity": integrity,
                "remainingRecoveries": 0,
                "partialRowPresent": False,
            },
        )
    finally:
        _close_client(client)


def _child_command(
    phase: str,
    *,
    workspace: Path,
    artifact_dir: Path,
    phase_token: str,
) -> list[str]:
    """Build the command line for one child phase.

    Every value is attached to its option with ``=``. Separating them with a
    space breaks whenever a value begins with a hyphen, because the argument
    parser then reads that value as the next option and reports
    ``expected one argument``. That is not hypothetical: the phase token used
    to be generated with ``secrets.token_urlsafe``, whose alphabet includes
    ``-``, so roughly one run in sixty-four launched a child that refused to
    start and failed the whole probe. The token is now hexadecimal as well, so
    the two defences are independent.
    """
    return [
        sys.executable,
        "-m",
        "mempalace.receipt_restart_probe",
        f"--phase={phase}",
        f"--workspace={workspace}",
        f"--phase-artifact-dir={artifact_dir}",
        f"--phase-token={phase_token}",
    ]


def _run_child(
    phase: str,
    *,
    workspace: Path,
    artifact_dir: Path,
    phase_token: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    command = _child_command(
        phase,
        workspace=workspace,
        artifact_dir=artifact_dir,
        phase_token=phase_token,
    )
    stdout_path = artifact_dir / f"phase-{phase}.stdout.log"
    stderr_path = artifact_dir / f"phase-{phase}.stderr.log"
    env = os.environ.copy()
    repo_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (repo_root, env.get("PYTHONPATH", "")) if item
    )
    env["PYTHONUNBUFFERED"] = "1"
    started = time.monotonic()
    timed_out = False
    exit_code: Optional[int] = None
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            completed = subprocess.run(
                command,
                cwd=repo_root,
                env=env,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout_seconds,
                check=False,
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
    return {
        "phase": phase,
        "exitCode": exit_code,
        "timedOut": timed_out,
        "durationSeconds": round(time.monotonic() - started, 3),
        "stdoutLog": str(stdout_path),
        "stderrLog": str(stderr_path),
    }


def _default_artifact_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".local" / "share"
    return base / "MemSys" / "eval-artifacts" / "mempalace-write-receipt-restart"


def run_probe(
    *,
    artifact_root: Optional[Path] = None,
    scratch_parent: Optional[Path] = None,
    phase_timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Run the four-process disposable proof and return its final artifact."""
    if phase_timeout_seconds < 1:
        raise ValueError("phase timeout must be positive")
    started_at = _utc_now()
    started = time.monotonic()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{uuid.uuid4().hex[:8]}"
    root = Path(artifact_root) if artifact_root is not None else _default_artifact_root()
    artifact_dir = root / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    parent = Path(scratch_parent) if scratch_parent is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(
            prefix=f"mempalace-restart-{run_id}-",
            dir=str(parent) if parent is not None else None,
        )
    )
    # Hexadecimal, not URL-safe base64: the URL-safe alphabet contains "-",
    # and a token starting with "-" was read as an option by the child's
    # argument parser, which then refused to start. Same 256 bits of entropy.
    phase_token = secrets.token_hex(32)
    _durable_json(
        workspace / WORKSPACE_MARKER,
        {
            "schema": WORKSPACE_MARKER_SCHEMA,
            "disposable": True,
            "runId": run_id,
            "phaseToken": phase_token,
            "createdAt": _utc_now(),
        },
    )

    phases: list[dict[str, Any]] = []
    expected = {"seed": 0, "interrupt": EXPECTED_INTERRUPT_EXIT, "recover": 0, "verify": 0}
    failure: Optional[str] = None
    for phase in ("seed", "interrupt", "recover", "verify"):
        result = _run_child(
            phase,
            workspace=workspace,
            artifact_dir=artifact_dir,
            phase_token=phase_token,
            timeout_seconds=phase_timeout_seconds,
        )
        result["expectedExitCode"] = expected[phase]
        result["exitMatched"] = not result["timedOut"] and result["exitCode"] == expected[phase]
        phases.append(result)
        if not result["exitMatched"]:
            failure = f"phase {phase} did not reach expected exit boundary"
            break
        phase_evidence = _phase_path(artifact_dir, phase)
        if not phase_evidence.is_file():
            failure = f"phase {phase} did not publish evidence"
            break

    cleanup: dict[str, Any] = {
        "attempted": False,
        "succeeded": False,
        "workspacePreserved": True,
        "workspacePath": str(workspace),
    }
    if failure is None:
        cleanup["attempted"] = True
        try:
            shutil.rmtree(workspace)
            cleanup["succeeded"] = not workspace.exists()
        except OSError as exc:
            failure = f"disposable workspace cleanup failed: {type(exc).__name__}"
    cleanup["workspacePreserved"] = workspace.exists()
    cleanup["workspacePath"] = str(workspace) if workspace.exists() else None

    verify = (
        _read_json(_phase_path(artifact_dir, "verify"))
        if failure is None and _phase_path(artifact_dir, "verify").is_file()
        else {}
    )
    payload = {
        "schema": SCHEMA,
        "status": "ok" if failure is None and cleanup["succeeded"] else "failed",
        "runId": run_id,
        "startedAt": started_at,
        "finishedAt": _utc_now(),
        "durationSeconds": round(time.monotonic() - started, 3),
        "versions": {
            "python": sys.version.split()[0],
            "chromadb": getattr(chromadb, "__version__", "unknown"),
        },
        "scope": {
            "mutationTarget": "disposable-synthetic-chroma",
            "livePalaceTouched": False,
            "historicalSourcesRead": False,
            "providerCalls": False,
            "networkRequired": False,
            "railwayAccessed": False,
            "disposableWorkspaceMarkerValidated": failure is None,
        },
        "processBoundary": {
            "strictlySequential": True,
            "overlappingPersistentClients": False,
            "expectedHardExitCode": EXPECTED_INTERRUPT_EXIT,
            "expectedHardExitObserved": any(
                item["phase"] == "interrupt" and item["exitMatched"] for item in phases
            ),
        },
        "phases": phases,
        "recovery": {
            "action": verify.get("phase") and "restore",
            "restoredRows": 1 if verify.get("status") == "ok" else 0,
            "remainingRecoveries": verify.get("remainingRecoveries"),
            "vectorQueryTopId": verify.get("vectorQueryTopId"),
            "sqliteIntegrity": verify.get("sqliteIntegrity"),
        },
        "cleanup": cleanup,
        "artifactDirectory": str(artifact_dir),
        "failure": failure,
    }
    final_path = artifact_dir / "probe-result.json"
    payload["artifactPath"] = str(final_path)
    _durable_json(final_path, payload)
    return payload


def _run_phase(phase: str, workspace: Path, artifact_dir: Path, phase_token: str) -> None:
    _validate_phase_workspace(workspace, phase_token, seed=phase == "seed")
    handlers = {
        "seed": _phase_seed,
        "interrupt": _phase_interrupt,
        "recover": _phase_recover,
        "verify": _phase_verify,
    }
    handlers[phase](workspace, artifact_dir)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--scratch-parent", type=Path)
    parser.add_argument("--phase-timeout-seconds", type=int, default=120)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--phase",
        choices=("seed", "interrupt", "recover", "verify"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--workspace", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--phase-artifact-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--phase-token", help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.phase:
        if args.workspace is None or args.phase_artifact_dir is None or args.phase_token is None:
            raise SystemExit(
                "internal --phase requires --workspace, --phase-artifact-dir, and --phase-token"
            )
        _run_phase(args.phase, args.workspace, args.phase_artifact_dir, args.phase_token)
        return 0
    result = run_probe(
        artifact_root=args.artifact_root,
        scratch_parent=args.scratch_parent,
        phase_timeout_seconds=args.phase_timeout_seconds,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"{result['status']}: {result['artifactPath']}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

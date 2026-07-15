import json
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "docs" / "research" / "managed-write-boundary-dispositions-2026-07-14.json"
EXPECTED_IDS = {
    "backend-open-time-mutation",
    "closet-regeneration",
    "compression-derived-closets",
    "deduplicate-drawers",
    "diary-ingest",
    "knowledge-graph-sqlite",
    "legacy-cli-repair-implementation",
    "mcp-diary-write",
    "mcp-direct-collection-cache",
    "mcp-drawer-mutations",
    "migration-rebuild-swap",
    "non-palace-operational-artifacts",
    "project-sidecar-files",
    "public-direct-collection-api",
    "receipt-optional-core-miners",
    "repair-max-sequence-id",
    "repair-replay-rebuild",
    "repair-row-pruning",
    "sweeper-jsonl-ingest",
    "topic-tunnel-registry",
    "unmanaged-palace-context",
}


def test_managed_write_boundary_manifest_is_complete_and_actionable():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema"] == "mempalace-managed-write-boundary-disposition/v1"
    assert manifest["status"] == "decisions-recorded-implementation-open"
    assert manifest["ownerIssue"] == "https://github.com/iMelki/mempalace/issues/22"

    entries = manifest["entries"]
    assert {entry["id"] for entry in entries} == EXPECTED_IDS
    assert len(entries) == len(EXPECTED_IDS)
    allowed = set(manifest["allowedDispositions"])
    counts = Counter(entry["disposition"] for entry in entries)
    assert set(counts) <= allowed
    assert counts == {
        "adapt-to-managed-receipts": 10,
        "retire-unmanaged-mutation-surface": 6,
        "explicitly-exclude-with-boundary": 5,
    }
    assert manifest["summary"] == {
        "total": 21,
        "adaptToManagedReceipts": 10,
        "retireUnmanagedMutationSurface": 6,
        "explicitlyExcludeWithBoundary": 5,
    }
    assert manifest["implementationProgress"] == {
        "adaptationsMetOnDev": 3,
        "adaptationsRemaining": 7,
        "completedAdaptationIds": [
            "diary-ingest",
            "mcp-diary-write",
            "mcp-drawer-mutations",
        ],
    }

    diary_ingest = next(entry for entry in entries if entry["id"] == "diary-ingest")
    assert diary_ingest["currentState"] == "managed-receipt-adapter-on-dev"
    assert diary_ingest["acceptanceStatus"] == "met-on-dev"

    for entry in entries:
        assert entry["surface"]
        assert entry["reason"]
        assert entry["currentState"]
        assert entry["requiredAcceptance"]
        assert entry["evidence"]
        for evidence in entry["evidence"]:
            assert evidence["line"] > 0
            assert (REPO_ROOT / evidence["path"]).is_file(), entry["id"]
        if entry["disposition"] == "adapt-to-managed-receipts":
            assert entry["managedRepresentation"]
        elif entry["disposition"] == "retire-unmanaged-mutation-surface":
            assert entry["retirementTarget"]
        else:
            assert entry["boundaryContract"].endswith("/v1")

"""Immutable evaluation-corpus identity contract for MemSys readbacks.

This is deliberately not a live status/count projection.  A caller may expose
the contract only after it has created an immutable logical-inventory manifest
with an attested inventory digest.  The HTTP server retains the existing
fail-closed ``unavailable`` state when no validated manifest is supplied.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


EVALUATION_CORPUS_MANIFEST_SCHEMA = "mempalace-evaluation-corpus-manifest/v1"
CORPUS_GENERATION_SCHEMA = "mempalace-corpus-generation/v1"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_MANIFEST_FIELDS = {
    "schema",
    "dataPlaneId",
    "inventorySha256",
    "scopeSha256",
    "sourceRevision",
    "processingSourceRevision",
    "capturedAtUtc",
    "itemCount",
    "corpusRevision",
}


class EvaluationCorpusManifestError(ValueError):
    """Raised when an evaluation corpus identity is malformed or unbound."""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def sha256_identity(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _required_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise EvaluationCorpusManifestError(f"{label} must be a sha256 identity")
    return value


def _captured_at(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvaluationCorpusManifestError("capturedAtUtc must be a UTC timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationCorpusManifestError("capturedAtUtc must be a UTC timestamp") from exc
    return value


def validate_evaluation_corpus_manifest(
    value: Mapping[str, Any], *, expected_data_plane_id: str | None
) -> dict[str, object]:
    """Validate a startup-supplied, secret-free immutable corpus manifest.

    ``expected_data_plane_id`` must be the service's startup-bound identity.
    The manifest cannot bind a different palace or silently activate when the
    service itself has no data-plane identity.
    """

    if not isinstance(value, Mapping):
        raise EvaluationCorpusManifestError("evaluation corpus manifest must be an object")
    manifest = dict(value)
    if set(manifest) != _MANIFEST_FIELDS:
        raise EvaluationCorpusManifestError("evaluation corpus manifest fields are unsupported")
    if manifest.get("schema") != EVALUATION_CORPUS_MANIFEST_SCHEMA:
        raise EvaluationCorpusManifestError("evaluation corpus manifest schema is unsupported")
    data_plane_id = _required_sha256(manifest.get("dataPlaneId"), label="dataPlaneId")
    if expected_data_plane_id is None or data_plane_id != expected_data_plane_id:
        raise EvaluationCorpusManifestError("evaluation corpus manifest dataPlaneId is unbound")
    inventory = _required_sha256(manifest.get("inventorySha256"), label="inventorySha256")
    scope = _required_sha256(manifest.get("scopeSha256"), label="scopeSha256")
    source = _required_sha256(manifest.get("sourceRevision"), label="sourceRevision")
    processing_source = _required_sha256(manifest.get("processingSourceRevision"), label="processingSourceRevision")
    captured_at = _captured_at(manifest.get("capturedAtUtc"))
    item_count = manifest.get("itemCount")
    if type(item_count) is not int or item_count < 0:
        raise EvaluationCorpusManifestError("itemCount must be a non-negative integer")
    material = {
        "schema": EVALUATION_CORPUS_MANIFEST_SCHEMA,
        "dataPlaneId": data_plane_id,
        "inventorySha256": inventory,
        "scopeSha256": scope,
        "sourceRevision": source,
        "processingSourceRevision": processing_source,
        "itemCount": item_count,
    }
    corpus_revision = _required_sha256(manifest.get("corpusRevision"), label="corpusRevision")
    if corpus_revision != sha256_identity(material):
        raise EvaluationCorpusManifestError("corpusRevision does not bind manifest material")
    return {
        "schema": CORPUS_GENERATION_SCHEMA,
        "status": "complete",
        "corpusRevision": corpus_revision,
        "scope": "evaluation-manifest",
        "capturedAtUtc": captured_at,
        "itemCount": item_count,
        "inventorySha256": inventory,
    }


def load_evaluation_corpus_manifest(
    path: Path, *, expected_data_plane_id: str | None
) -> dict[str, Any]:
    """Load and validate one startup-only manifest without exposing its path."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationCorpusManifestError("evaluation corpus manifest is unreadable") from exc
    if not isinstance(value, Mapping):
        raise EvaluationCorpusManifestError("evaluation corpus manifest must be an object")
    validate_evaluation_corpus_manifest(value, expected_data_plane_id=expected_data_plane_id)
    return dict(value)

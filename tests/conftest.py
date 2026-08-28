"""
conftest.py — Shared fixtures for MemPalace tests.

Provides isolated palace and knowledge graph instances so tests never
touch the user's real data or leak temp files on failure.

HOME is redirected to a temp directory at module load time — before any
mempalace imports — so that module-level initialisations (e.g.
``_kg = KnowledgeGraph()`` in mcp_server) write to a throwaway location
instead of the real user profile.
"""

import json
import os
import hashlib
import math
import re
import shutil
import tempfile
import time

# ── Isolate HOME before any mempalace imports ──────────────────────────
_original_env = {}
_session_tmp = tempfile.mkdtemp(prefix="mempalace_session_")

for _var in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
    _original_env[_var] = os.environ.get(_var)

os.environ["HOME"] = _session_tmp
os.environ["USERPROFILE"] = _session_tmp
os.environ["HOMEDRIVE"] = os.path.splitdrive(_session_tmp)[0] or "C:"
os.environ["HOMEPATH"] = os.path.splitdrive(_session_tmp)[1] or _session_tmp

# mempalace#41: give the managed-write readback loops (write_receipts.py) a
# generous budget under test rather than the 5s production default, so a
# loaded full-suite run has more headroom before the bounded retry gives up.
# `setdefault` so a developer's own override (e.g. to force 0.0 and exercise
# the deadline path outside `monkeypatch`) still wins. Individual tests that
# `monkeypatch.setattr` the module constant directly take precedence over
# both, since that assignment happens after import time.
os.environ.setdefault("MEMPALACE_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS", "20")

# mempalace#50: bound OpenMP/OpenBLAS/ONNX/Rayon pools BEFORE chromadb
# imports those native libraries. Later setdefault calls are no-ops.
from mempalace.native_lifecycle import (  # noqa: E402
    NativeLifecycleMonitor,
    apply_native_thread_bounds,
    bound_native_lifecycle_enabled,
    close_chroma_client,
    install_persistent_client_tracker,
    release_native_sessions,
)

apply_native_thread_bounds()

# Now it is safe to import mempalace modules that trigger initialisation.
import chromadb  # noqa: E402
import pytest  # noqa: E402

from mempalace.config import MempalaceConfig  # noqa: E402
from mempalace.knowledge_graph import KnowledgeGraph  # noqa: E402

if bound_native_lifecycle_enabled():
    install_persistent_client_tracker()

_NATIVE_LIFECYCLE_MONITOR = NativeLifecycleMonitor()


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class _DeterministicEmbeddingFunction:
    @staticmethod
    def name() -> str:
        return "default"

    def _embed_many(self, input):
        embeddings = []
        for item in input:
            text = item if isinstance(item, str) else ""
            vector = [0.0] * 32
            tokens = _TOKEN_RE.findall(text.lower())
            if not tokens:
                vector[0] = 1.0
            else:
                for token in tokens:
                    digest = hashlib.sha256(token.encode("utf-8")).digest()
                    vector[digest[0] % len(vector)] += 1.0
                    vector[digest[1] % len(vector)] += 0.5

            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            embeddings.append([value / norm for value in vector])
        return embeddings

    def __call__(self, input):
        return self._embed_many(input)

    def embed_documents(self, input):
        return self._embed_many(input)

    def embed_query(self, input):
        if isinstance(input, str):
            return self._embed_many([input])
        return self._embed_many(input)


_TEST_EMBEDDING_FUNCTION = _DeterministicEmbeddingFunction()


@pytest.fixture(autouse=True)
def _reset_mcp_cache():
    """Reset process-wide caches so no test leaks state into the next one.

    mempalace#41 measured two never-reset module-level singletons that
    accumulate for the whole test session:

    * ``palace._DEFAULT_BACKEND`` — its ``_clients``/``_freshness`` dicts
      held 110 live ``PersistentClient`` objects (plus their SQLite handles
      and in-RAM HNSW segments) by the end of one full-suite run, since
      nothing ever called ``close_palace``/``close`` between tests.
    * ``miner._ENTITY_REGISTRY_CACHE`` — mtime-gated against
      ``~/.mempalace/known_entities.json``, which resolves against the
      session-shared temp ``HOME`` set up above. A predecessor test's write
      could silently change a later test's closet documents and receipt
      entity-extraction digest.

    Both are reset here alongside the pre-existing MCP client/collection
    cache and ``ChromaBackend._quarantined_paths`` clear.

    mempalace#50 extends that reset into an explicit native-session
    close/dispose: tracked ``PersistentClient`` objects, an already-imported
    MCP client cache, and cached ONNX embedding sessions are closed through
    their supported APIs rather than dropped.
    """

    def _clear_cache():
        if bound_native_lifecycle_enabled():
            # Close tracked PersistentClients, the default backend cache,
            # an already-imported MCP client cache, and ONNX EF sessions
            # before dropping Python references (mempalace#50).
            release_native_sessions()
        else:
            try:
                from mempalace import mcp_server

                mcp_server._client_cache = None
                mcp_server._collection_cache = None
            except (ImportError, AttributeError):
                pass
            try:
                from mempalace import palace

                palace.reset_default_backend()
            except (ImportError, AttributeError):
                pass
        try:
            # Reset the per-process quarantine gate so tests don't leak
            # state through ChromaBackend._quarantined_paths.
            from mempalace.backends.chroma import ChromaBackend

            ChromaBackend._quarantined_paths.clear()
        except (ImportError, AttributeError):
            pass
        try:
            from mempalace import miner

            miner.reset_entity_registry_cache()
        except (ImportError, AttributeError):
            pass

    _clear_cache()
    yield
    _clear_cache()


@pytest.fixture(autouse=True)
def _force_test_embedding_function(monkeypatch):
    from mempalace.backends.chroma import ChromaBackend

    monkeypatch.setattr(
        ChromaBackend,
        "_resolve_embedding_function",
        staticmethod(lambda: _TEST_EMBEDDING_FUNCTION),
    )


@pytest.fixture
def test_embedding_function():
    return _TEST_EMBEDDING_FUNCTION


@pytest.fixture(scope="session", autouse=True)
def _isolate_home():
    """Ensure HOME points to a temp dir for the entire test session.

    The env vars were already set at module level (above) so that
    module-level initialisations are captured.  This fixture simply
    restores the originals on teardown and cleans up the temp dir.
    """
    yield
    for var, orig in _original_env.items():
        if orig is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = orig
    shutil.rmtree(_session_tmp, ignore_errors=True)


@pytest.fixture
def tmp_dir():
    """Create and auto-cleanup a temporary directory."""
    d = tempfile.mkdtemp(prefix="mempalace_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def palace_path(tmp_dir):
    """Path to an empty palace directory inside tmp_dir."""
    p = os.path.join(tmp_dir, "palace")
    os.makedirs(p)
    return p


@pytest.fixture
def config(tmp_dir, palace_path):
    """A MempalaceConfig pointing at the temp palace."""
    cfg_dir = os.path.join(tmp_dir, "config")
    os.makedirs(cfg_dir)
    import json

    with open(os.path.join(cfg_dir, "config.json"), "w") as f:
        json.dump({"palace_path": palace_path}, f)
    return MempalaceConfig(config_dir=cfg_dir)


@pytest.fixture
def collection(palace_path):
    """A ChromaDB collection pre-seeded in the temp palace."""
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection(
        "mempalace_drawers",
        metadata={"hnsw:space": "cosine"},
        embedding_function=_TEST_EMBEDDING_FUNCTION,
    )
    yield col
    try:
        client.delete_collection("mempalace_drawers")
    finally:
        close_chroma_client(client)


@pytest.fixture
def seeded_collection(collection):
    """Collection with a handful of representative drawers."""
    collection.add(
        ids=[
            "drawer_proj_backend_aaa",
            "drawer_proj_backend_bbb",
            "drawer_proj_frontend_ccc",
            "drawer_notes_planning_ddd",
        ],
        documents=[
            "The authentication module uses JWT tokens for session management. "
            "Tokens expire after 24 hours. Refresh tokens are stored in HttpOnly cookies.",
            "Database migrations are handled by Alembic. We use PostgreSQL 15 "
            "with connection pooling via pgbouncer.",
            "The React frontend uses TanStack Query for server state management. "
            "All API calls go through a centralized fetch wrapper.",
            "Sprint planning: migrate auth to passkeys by Q3. "
            "Evaluate ChromaDB alternatives for vector search.",
        ],
        metadatas=[
            {
                "wing": "project",
                "room": "backend",
                "source_file": "auth.py",
                "chunk_index": 0,
                "added_by": "miner",
                "filed_at": "2026-01-01T00:00:00",
            },
            {
                "wing": "project",
                "room": "backend",
                "source_file": "db.py",
                "chunk_index": 0,
                "added_by": "miner",
                "filed_at": "2026-01-02T00:00:00",
            },
            {
                "wing": "project",
                "room": "frontend",
                "source_file": "App.tsx",
                "chunk_index": 0,
                "added_by": "miner",
                "filed_at": "2026-01-03T00:00:00",
            },
            {
                "wing": "notes",
                "room": "planning",
                "source_file": "sprint.md",
                "chunk_index": 0,
                "added_by": "miner",
                "filed_at": "2026-01-04T00:00:00",
            },
        ],
    )
    return collection


@pytest.fixture
def kg(tmp_dir):
    """An isolated KnowledgeGraph using a temp SQLite file."""
    db_path = os.path.join(tmp_dir, "test_kg.sqlite3")
    graph = KnowledgeGraph(db_path=db_path)
    yield graph
    graph.close()


@pytest.fixture
def seeded_kg(kg):
    """KnowledgeGraph pre-loaded with sample triples."""
    kg.add_entity("Alice", entity_type="person")
    kg.add_entity("Max", entity_type="person")
    kg.add_entity("swimming", entity_type="activity")
    kg.add_entity("chess", entity_type="activity")

    kg.add_triple("Alice", "parent_of", "Max", valid_from="2015-04-01")
    kg.add_triple("Max", "does", "swimming", valid_from="2025-01-01")
    kg.add_triple("Max", "does", "chess", valid_from="2024-06-01")
    kg.add_triple("Alice", "works_at", "Acme Corp", valid_from="2020-01-01", valid_to="2024-12-31")
    kg.add_triple("Alice", "works_at", "NewCo", valid_from="2025-01-01")

    return kg


# ── Failure diagnostics (mempalace#41 / #24) ────────────────────────────
#
# Two flaky-under-load occurrences in TestDiaryIngest each lost their
# traceback: the pre-push runner invoked pytest with just `-q`, and neither
# occurrence recorded attempt counts or elapsed time for the bounded-retry
# helpers on the suspected path. The two pieces below close that gap:
#
# 1. `pytest_runtest_makereport` attaches a sanitized section to the current
#    failed setup/call report before pytest emits that report.
# 2. `_readback_and_lock_diagnostics` wraps the two managed-write readback
#    helpers named in #41 (`_collection_rows_for_ids`,
#    `_verify_managed_write_readback`) to count calls and track elapsed
#    time, and resets then reads `palace.get_mine_lock_stats()` for the
#    current test's mine_lock attempt counters. The makereport hook attaches
#    a diagnostics section to the actual failed setup/call report before
#    pytest emits it. It adds only a bounded,
#    pseudonymous metrics section to pytest's own report. Full traceback text
#    and raw node IDs can contain source paths, fixture values, or assertion
#    data, so this fixture never materializes a separate local artifact.
_DIAGNOSTICS_SCHEMA = "mempalace-test-failure-diagnostics/v1"
_DIAGNOSTICS_MAX_BYTES = 4096


def _build_sanitized_failure_diagnostics(
    *,
    nodeid: str,
    phase: str,
    stats: dict,
    mine_lock_stats: dict,
) -> str:
    """Build one bounded metrics-only report section without raw test data."""
    artifact = {
        "schema": _DIAGNOSTICS_SCHEMA,
        "phase": phase,
        "test_id_sha256": hashlib.sha256(nodeid.encode("utf-8")).hexdigest(),
        "readback": {
            "collection_rows_for_ids_calls": int(stats["collection_rows_for_ids_calls"]),
            "collection_rows_for_ids_elapsed_max": float(
                stats["collection_rows_for_ids_elapsed_max"]
            ),
            "verify_managed_write_readback_calls": int(
                stats["verify_managed_write_readback_calls"]
            ),
            "verify_managed_write_readback_elapsed_max": float(
                stats["verify_managed_write_readback_elapsed_max"]
            ),
        },
        "mine_lock": {
            "attempts": int(mine_lock_stats["attempts"]),
            "successful_acquisitions": int(mine_lock_stats["successful_acquisitions"]),
            "failed_acquisitions": int(mine_lock_stats["failed_acquisitions"]),
            "max_attempt_seconds": float(mine_lock_stats["max_attempt_seconds"]),
            "max_successful_acquire_seconds": float(
                mine_lock_stats["max_successful_acquire_seconds"]
            ),
        },
    }
    encoded = json.dumps(artifact, allow_nan=False, ensure_ascii=True, sort_keys=True).encode(
        "utf-8"
    )
    if len(encoded) > _DIAGNOSTICS_MAX_BYTES:
        return '{"schema":"mempalace-test-failure-diagnostics/v1","status":"omitted-size-cap"}'
    return encoded.decode("utf-8")


def pytest_runtest_setup(item):
    """Record the current test before it runs so a killed pre-push child
    still has last-test/progress evidence on disk (mempalace#50)."""
    _NATIVE_LIFECYCLE_MONITOR.note_test(item.nodeid, "setup")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    diagnostic_state = getattr(item, "_mempalace_failure_diagnostic_state", None)
    if diagnostic_state is not None and rep.when in {"setup", "call"} and rep.failed:
        diagnostics = _build_sanitized_failure_diagnostics(
            nodeid=item.nodeid,
            phase=rep.when,
            stats=diagnostic_state["stats"],
            mine_lock_stats=diagnostic_state["palace"].get_mine_lock_stats(),
        )
        # pytest emits this report after the hook returns. Appending directly
        # to the current TestReport is intentionally different from calling
        # item.add_report_section during fixture teardown, which would land on
        # a later teardown report and be invisible beside the actual failure.
        rep.sections.append(("mempalace safe diagnostics", diagnostics))
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture(autouse=True)
def _readback_and_lock_diagnostics(request, monkeypatch):
    try:
        from mempalace import palace, write_receipts as wr
    except ImportError:
        yield
        return

    stats = {
        "collection_rows_for_ids_calls": 0,
        "collection_rows_for_ids_elapsed_max": 0.0,
        "verify_managed_write_readback_calls": 0,
        "verify_managed_write_readback_elapsed_max": 0.0,
    }

    orig_rows = wr._collection_rows_for_ids
    orig_verify = wr._verify_managed_write_readback

    def _wrapped_rows(*args, **kwargs):
        started = time.monotonic()
        try:
            return orig_rows(*args, **kwargs)
        finally:
            elapsed = time.monotonic() - started
            stats["collection_rows_for_ids_calls"] += 1
            if elapsed > stats["collection_rows_for_ids_elapsed_max"]:
                stats["collection_rows_for_ids_elapsed_max"] = elapsed

    def _wrapped_verify(*args, **kwargs):
        started = time.monotonic()
        try:
            return orig_verify(*args, **kwargs)
        finally:
            elapsed = time.monotonic() - started
            stats["verify_managed_write_readback_calls"] += 1
            if elapsed > stats["verify_managed_write_readback_elapsed_max"]:
                stats["verify_managed_write_readback_elapsed_max"] = elapsed

    monkeypatch.setattr(wr, "_collection_rows_for_ids", _wrapped_rows)
    monkeypatch.setattr(wr, "_verify_managed_write_readback", _wrapped_verify)

    # The counters are diagnostic-only and process-local in pytest. Resetting
    # them before this test prevents an earlier contention test from being
    # misattributed to a later failure.
    palace.reset_mine_lock_stats()
    setattr(
        request.node,
        "_mempalace_failure_diagnostic_state",
        {"palace": palace, "stats": stats},
    )
    try:
        yield
    finally:
        delattr(request.node, "_mempalace_failure_diagnostic_state")


def pytest_runtest_teardown(item):
    _NATIVE_LIFECYCLE_MONITOR.note_test(item.nodeid, "teardown")


def pytest_sessionfinish(session, exitstatus):
    """Release leftover native sessions and persist the caller receipt."""
    if bound_native_lifecycle_enabled():
        release_native_sessions()
    _NATIVE_LIFECYCLE_MONITOR.write_receipt(final=True)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    receipt = _NATIVE_LIFECYCLE_MONITOR.build_receipt(final=True)
    terminalreporter.write_sep("-", "mempalace native lifecycle (#50)")
    terminalreporter.write_line(
        "peak_threads={threads} peak_handles={handles} "
        "private_bytes={private} duration_s={duration} last_test={last} "
        "receipt={receipt}".format(
            threads=receipt["peak"]["threads"],
            handles=receipt["peak"]["handles"],
            private=receipt["peak"]["private_bytes"],
            duration=receipt["duration_seconds"],
            last=receipt["last_test"] or "(none)",
            receipt=_NATIVE_LIFECYCLE_MONITOR.receipt_path,
        )
    )

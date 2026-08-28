"""Bound Chroma/ONNX native thread and session lifecycle (mempalace#50).

The full-suite pre-push path previously accumulated waiting Chroma/ONNX/OpenBLAS
threads because raw ``PersistentClient`` objects, the MCP client cache, and
cached embedding sessions were dropped without going through their supported
close/dispose APIs. This module is the single owning boundary for:

* process-wide native thread-pool env bounds (must run before those libraries
  import);
* tracking and closing Chroma clients;
* disposing ONNX embedding sessions;
* sampling thread/handle/memory counters for the caller receipt.

Production mining still uses process-lifetime embedding caches. Tests and
shutdown paths call :func:`release_native_sessions`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
import weakref
from typing import Any, Optional

logger = logging.getLogger(__name__)

RECEIPT_SCHEMA = "mempalace-test-native-lifecycle/v1"
RECEIPT_ENV = "MEMPALACE_TEST_LIFECYCLE_RECEIPT"
BOUND_ENV = "MEMPALACE_BOUND_NATIVE_LIFECYCLE"

# Bound NEW native pools. These are read at library import time, so
# :func:`apply_native_thread_bounds` must run before chromadb/onnx/scipy load.
NATIVE_THREAD_BOUND_ENV = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "RAYON_NUM_THREADS": "1",
    "TOKENIZERS_PARALLELISM": "false",
}

_SESSION_ATTRS = (
    "_session",
    "session",
    "ort_session",
    "_ort_session",
    "model",
    "_model",
)

_closed_clients: weakref.WeakSet[Any] = weakref.WeakSet()
_GLOBAL_REGISTRY: Optional["NativeSessionRegistry"] = None
_TRACKER_INSTALLED = False


def bound_native_lifecycle_enabled() -> bool:
    """Return whether the suite should track and release native sessions."""
    raw = os.environ.get(BOUND_ENV, "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def apply_native_thread_bounds(*, overwrite: bool = False) -> dict[str, str]:
    """Set conservative native thread-pool env vars if unset.

    Does not import chromadb or onnxruntime. ``setdefault`` keeps an explicit
    operator override. Returns the effective mapping.
    """
    applied: dict[str, str] = {}
    for key, value in NATIVE_THREAD_BOUND_ENV.items():
        if overwrite or key not in os.environ:
            os.environ[key] = value
        applied[key] = os.environ[key]
    return applied


def close_chroma_client(client: Any, *, strict: bool = False) -> bool:
    """Close a Chroma client through its supported lifecycle API.

    Idempotent: a second call on the same object is a no-op. Teardown
    callers leave ``strict=False`` so a close failure cannot mask the
    originating test error. Production backend close uses ``strict=True``.
    """
    if client is None:
        return False
    try:
        if client in _closed_clients:
            return False
    except TypeError:
        pass
    close = getattr(client, "close", None)
    if not callable(close):
        return False
    try:
        close()
    except Exception:
        if strict:
            raise
        logger.debug("closing a Chroma client failed", exc_info=True)
    try:
        _closed_clients.add(client)
    except TypeError:
        pass
    return True


def dispose_onnx_owner(owner: Any) -> bool:
    """End an ONNX Runtime session held by an embedding-function owner."""
    if owner is None:
        return False
    disposed = False
    ender = getattr(owner, "end_session", None)
    if callable(ender):
        try:
            ender()
            disposed = True
        except Exception:
            logger.debug("owner.end_session failed", exc_info=True)
    for name in _SESSION_ATTRS:
        session = getattr(owner, name, None)
        if session is None or callable(session):
            continue
        session_ender = getattr(session, "end_session", None)
        if callable(session_ender):
            try:
                session_ender()
                disposed = True
            except Exception:
                logger.debug("ONNX session end_session failed", exc_info=True)
        try:
            setattr(owner, name, None)
            disposed = True
        except Exception:
            logger.debug("clearing ONNX session attribute failed", exc_info=True)
    return disposed


class NativeSessionRegistry:
    """Strong registry of clients/sessions that must be closed at a boundary."""

    def __init__(self) -> None:
        self._owners: list[Any] = []

    def track(self, owner: Any) -> Any:
        if owner is not None:
            self._owners.append(owner)
        return owner

    def live_owners(self) -> list[Any]:
        live = []
        for owner in self._owners:
            try:
                if owner in _closed_clients:
                    continue
            except TypeError:
                pass
            live.append(owner)
        return live

    def live_count(self) -> int:
        return len(self.live_owners())

    def release(self) -> int:
        released = 0
        remaining: list[Any] = []
        for owner in self._owners:
            closed = close_chroma_client(owner)
            disposed = dispose_onnx_owner(owner)
            already_closed = False
            try:
                already_closed = owner in _closed_clients
            except TypeError:
                already_closed = False
            if closed or disposed:
                released += 1
            elif not already_closed:
                remaining.append(owner)
        self._owners = remaining
        return released


def global_registry() -> NativeSessionRegistry:
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = NativeSessionRegistry()
    return _GLOBAL_REGISTRY


def install_persistent_client_tracker(registry: Optional[NativeSessionRegistry] = None) -> bool:
    """Wrap ``chromadb.PersistentClient`` so every constructed client is tracked.

    Returns True when the wrapper was installed on this call. Safe to call
    more than once. No-op when lifecycle bounding is disabled.
    """
    global _TRACKER_INSTALLED
    if not bound_native_lifecycle_enabled() or _TRACKER_INSTALLED:
        return False
    try:
        import chromadb
    except ImportError:
        return False
    target = registry or global_registry()
    original = chromadb.PersistentClient
    if getattr(original, "_mempalace_lifecycle_tracked", False):
        _TRACKER_INSTALLED = True
        return False

    def tracked_persistent_client(*args, **kwargs):
        client = original(*args, **kwargs)
        target.track(client)
        return client

    tracked_persistent_client._mempalace_lifecycle_tracked = True  # type: ignore[attr-defined]
    tracked_persistent_client._mempalace_lifecycle_original = original  # type: ignore[attr-defined]
    chromadb.PersistentClient = tracked_persistent_client
    _TRACKER_INSTALLED = True
    return True


def release_native_sessions(registry: Optional[NativeSessionRegistry] = None) -> dict[str, int]:
    """Close tracked Chroma/ONNX owners and process-wide cached sessions."""
    target = registry if registry is not None else global_registry()
    released_clients = target.release()

    try:
        from mempalace import palace

        palace.reset_default_backend()
    except Exception:
        logger.debug("reset_default_backend during native release failed", exc_info=True)

    mcp_server = sys.modules.get("mempalace.mcp_server")
    if mcp_server is not None:
        client = getattr(mcp_server, "_client_cache", None)
        try:
            mcp_server._client_cache = None
            mcp_server._collection_cache = None
        except Exception:
            logger.debug("clearing MCP client cache failed", exc_info=True)
        close_chroma_client(client)

    try:
        from mempalace.embedding import reset_embedding_function_cache

        reset_embedding_function_cache()
    except Exception:
        logger.debug("reset_embedding_function_cache failed", exc_info=True)

    return {"released_clients": released_clients, "live_clients": target.live_count()}


def _proc_status_int(field: str) -> Optional[int]:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(field):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def sample_native_resources() -> dict[str, int]:
    """Sample thread, handle, and memory counters without retaining paths."""
    python_threads = int(threading.active_count())
    threads = python_threads
    handles = 0
    rss_bytes = 0
    private_bytes = 0

    try:
        import psutil

        process = psutil.Process()
        threads = int(process.num_threads())
        rss_bytes = int(process.memory_info().rss)
        full = getattr(process, "memory_full_info", None)
        if callable(full):
            try:
                info = full()
                private_bytes = int(getattr(info, "uss", 0) or getattr(info, "private", 0) or 0)
            except Exception:
                private_bytes = rss_bytes
        else:
            private_bytes = rss_bytes
        num_fds = getattr(process, "num_fds", None)
        num_handles = getattr(process, "num_handles", None)
        if callable(num_fds):
            handles = int(num_fds())
        elif callable(num_handles):
            handles = int(num_handles())
    except Exception:
        proc_threads = _proc_status_int("Threads:")
        if proc_threads is not None:
            threads = proc_threads
        try:
            handles = len(os.listdir("/proc/self/fd"))
        except OSError:
            handles = 0
        vmrss = _proc_status_int("VmRSS:")
        if vmrss is not None:
            rss_bytes = vmrss * 1024
            private_bytes = rss_bytes

    if private_bytes <= 0:
        private_bytes = rss_bytes

    return {
        "python_threads": python_threads,
        "threads": threads,
        "handles": handles,
        "rss_bytes": rss_bytes,
        "private_bytes": private_bytes,
    }


def inspect_native_leak(
    registry: NativeSessionRegistry,
    baseline: Optional[dict[str, int]] = None,
) -> dict[str, Any]:
    """Return a leak report. ``live_owners > 0`` is the fail-closed signal."""
    current = sample_native_resources()
    report = {
        "live_owners": registry.live_count(),
        "resources": current,
    }
    if baseline is not None:
        report["thread_delta"] = current["threads"] - baseline["threads"]
        report["python_thread_delta"] = current["python_threads"] - baseline["python_threads"]
        report["handle_delta"] = current["handles"] - baseline["handles"]
    return report


class NativeLifecycleMonitor:
    """Accumulate peak resource samples and last-test progress for a run."""

    def __init__(self, receipt_path: Optional[str] = None) -> None:
        self.started = time.monotonic()
        self.baseline = sample_native_resources()
        self.peak = dict(self.baseline)
        self.last_test = ""
        self.last_test_phase = ""
        self.tests_completed = 0
        self.receipt_path = receipt_path or os.environ.get(RECEIPT_ENV) or ""
        if not self.receipt_path:
            handle, path = tempfile.mkstemp(prefix="mempalace-native-lifecycle-", suffix=".json")
            os.close(handle)
            self.receipt_path = path

    def _raise_peak(self, sample: dict[str, int]) -> None:
        for key, value in sample.items():
            if value > self.peak.get(key, 0):
                self.peak[key] = value

    def note_test(self, nodeid: str, phase: str) -> None:
        self.last_test = nodeid
        self.last_test_phase = phase
        self._raise_peak(sample_native_resources())
        if phase == "teardown":
            self.tests_completed += 1
        self.write_receipt(final=False)

    def build_receipt(self, *, final: bool) -> dict[str, Any]:
        current = sample_native_resources()
        self._raise_peak(current)
        return {
            "schema": RECEIPT_SCHEMA,
            "final": final,
            "duration_seconds": round(time.monotonic() - self.started, 3),
            "last_test": self.last_test,
            "last_test_phase": self.last_test_phase,
            "last_test_sha256": hashlib.sha256(self.last_test.encode("utf-8")).hexdigest()
            if self.last_test
            else "",
            "tests_completed": self.tests_completed,
            "baseline": self.baseline,
            "current": current,
            "peak": self.peak,
            "live_clients": global_registry().live_count(),
            "thread_bounds": apply_native_thread_bounds(),
            "lifecycle_bound": bound_native_lifecycle_enabled(),
        }

    def write_receipt(self, *, final: bool) -> dict[str, Any]:
        receipt = self.build_receipt(final=final)
        payload = json.dumps(receipt, allow_nan=False, ensure_ascii=True, sort_keys=True)
        directory = os.path.dirname(self.receipt_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = self.receipt_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_path, self.receipt_path)
        return receipt


class SyntheticNativePool:
    """Caller-faithful stand-in for a leaked Chroma/ONNX waiting-thread burst.

    Real ONNX/OpenBLAS pools are native and version-specific. This owner uses
    the same lifecycle contract those sessions should honor: ``close()`` joins
    the waiting workers. Tests use it so the leak gate does not need a
    900-second full suite or a live palace.
    """

    def __init__(self, workers: int = 8, name_prefix: str = "leaked-onnx") -> None:
        self._stop = threading.Event()
        self.threads = [
            threading.Thread(
                target=self._wait,
                name=f"{name_prefix}-{index}",
                daemon=False,
            )
            for index in range(workers)
        ]
        for thread in self.threads:
            thread.start()

    def _wait(self) -> None:
        self._stop.wait()

    def close(self) -> None:
        self._stop.set()
        for thread in self.threads:
            thread.join(timeout=2.0)

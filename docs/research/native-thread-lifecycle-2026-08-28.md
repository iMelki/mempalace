# Native Chroma/ONNX thread lifecycle (mempalace#50)

Date: 2026-08-28
Owners: [mempalace#50](https://github.com/iMelki/mempalace/issues/50)
Status: suite/pre-push bound implemented; no live palace or hosted service work

## Plain-English outcome

A full-suite pre-push can finish its pytest assertions and still stall inside
the protected 900-second caller with more than a thousand waiting native
threads. Direct `pytest` on the same tree was green. The missing bound was not
another retry: Chroma `PersistentClient` objects, the MCP client cache, and
ONNX embedding sessions were being dropped without `close()` / `end_session()`.

This change owns that lifecycle at the test/session boundary, caps new
OpenMP/OpenBLAS/Rayon/tokenizers pools at one thread before `chromadb`
imports, and writes an incremental last-test / peak-resource receipt so a
killed pre-push child still has progress evidence.

## What was leaking

- `#41` already resets `palace._DEFAULT_BACKEND` between tests. That does not
  close raw `chromadb.PersistentClient` objects created by fixtures and tests,
  and it nulls `mcp_server._client_cache` without calling `close()`.
- The shared `collection` fixture deleted the collection and `del`ed the
  client. Chroma reference-counts the per-folder system; only `close()`
  returns the reference.
- Cached `mempalace.embedding` ONNX owners are process-lifetime by design for
  production mining. The suite never disposed them.
- OpenBLAS / ONNX / Rayon pools are sized at import time. Unbounded defaults
  produce the 30/60-thread bursts seen on the stalled pre-push process.

## Contract

- Thread-pool env bounds run in `tests/conftest.py` **before** `import chromadb`.
  Operator overrides already in the environment are left alone.
- Every `PersistentClient` constructed after conftest import is tracked and
  closed at the autouse / session-finish boundary.
- `ChromaBackend._close_client` uses the same idempotent closer so a
  superseded-client release and a later suite teardown cannot double-close.
- ONNX owners are disposed through `end_session()` when present, then drop
  the session attribute.
- The caller receipt (`MEMPALACE_TEST_LIFECYCLE_RECEIPT`, otherwise a temp
  file) records peak threads, handles, private bytes, duration, last test,
  and live-client count. It is rewritten after each test so a timeout still
  leaves last-test evidence. No document text or palace paths are stored.
- Production mining behavior is unchanged: embedding caches stay
  process-lifetime unless a caller invokes `reset_embedding_function_cache()`.

## Proof

- Synthetic waiting-thread pool: leak is visible, `close()` returns Python
  thread counts to the pre-run baseline. No 900-second suite wait.
- Negative fixture: `tests/fixtures/native_lifecycle_unreleased.py` fails with
  `native-lifecycle-leak: unclosed chroma/onnx session`.
- Pre-push-shaped child: `pytest -q -p no:cacheprovider` on
  `tests/fixtures/native_lifecycle_bound_child.py` exits 0, writes a final
  receipt with `live_clients=0`, and prints the lifecycle summary.

## Out of scope

- Hosted MemPalace-service / Railway work
- Adding an MCP
- Private palace fallback
- Weakening Chroma hard-exit / recovery assertions
- Live palace, provider, or corpus mutation

## Maintainability

`mempalace/native_lifecycle.py` is the single owner. New suite code that
constructs a `PersistentClient` does not need a local `close()` as long as
conftest remains imported; prefer an explicit close at the fixture that
created the client. Do not skip or retry-mask Chroma recovery tests to hide
thread accumulation. Disable the bound only with
`MEMPALACE_BOUND_NATIVE_LIFECYCLE=0` while debugging.

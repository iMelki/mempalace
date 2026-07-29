# Deterministic project-mine progress contract

Date: 2026-07-29
Issue: `iMelki/mempalace#25`

## Purpose

Managed per-source receipts already make a repeated project source write
idempotent and recover interrupted rewrites. This contract adds the missing
outer source-order boundary so a long project mine can restart at the same
verified source index after a process kill, host crash, or reboot.

It does not authorize mining any configured palace. Tests use disposable
project trees and disposable Chroma stores.

## Research basis

The progress design follows established append-log recovery properties rather
than treating a successful process return as durability:

- POSIX `O_APPEND` makes positioning-at-end plus one write atomic for a regular
  local file; the Linux manual also warns that this guarantee does not extend
  cleanly to concurrent NFS appenders. MemPalace therefore retains one
  per-palace writer and does not claim a network-filesystem multiwriter
  guarantee: <https://man7.org/linux/man-pages/man2/open.2.html>.
- `write()` may legally be short, so the implementation rejects any write that
  does not persist the whole bounded record:
  <https://man7.org/linux/man-pages/man2/write.2.html>.
- Python exposes the explicit `fsync()` and `ftruncate()` primitives used for
  durable record publication and torn-tail recovery:
  <https://docs.python.org/3/library/os.html#os.fsync> and
  <https://docs.python.org/3/library/os.html#os.ftruncate>.
- SQLite WAL recovery validates records in order and stops at the first invalid
  frame/checksum, while commit is identified by a dedicated committed record.
  The MemPalace JSONL design adopts the narrower analogous rule that newline is
  the commit marker, each line is hash-chained, and bytes beyond the last
  newline are uncommitted:
  <https://sqlite.org/walformat.html> and
  <https://www.sqlite.org/wal.html>.

## Source manifest

`--plan-out PATH` creates, or reuses only when identical, one immutable JSON
manifest. `--manifest PATH` consumes it. Both apply to `projects` mode.

For large trees, pair planning with `--plan-progress-jsonl PATH`. The separate
fsynced, hash-chained planning journal checkpoints directory discovery and
each completed file descriptor, then resumes at the exact next item after a
kill or reboot. Its identity, torn-tail, concurrency, and bounded-state
rationale is documented in
[resumable-source-plan-contract-2026-07-29.md](resumable-source-plan-contract-2026-07-29.md).

The plan is sorted by normalized project-relative path and binds:

- zero-based source index and normalized relative path;
- byte length and nanosecond modification time;
- exact source-byte SHA-256;
- project-root identity without retaining the absolute root;
- project parser/mode and receipt-affecting configuration digest;
- durable-progress protocol revision and exact miner-module SHA-256.

Creation reads each regular source between two stat samples and fails if size
or modification time changes. Immediately before processing, the miner reads
the source under its per-source lock and compares the locked bytes and both
stat samples with the manifest. A mismatch raises `MineManifestDrift` before
that source's managed write begins.

## Progress authorization

`--progress-jsonl PATH` is append-only. Each record contains the manifest and
item digests, source index, next-source index, target-palace HMAC source
identity, terminal receipt UUID/disposition, represented count, timestamp, and
a SHA-256 link to the prior progress record.

It deliberately excludes absolute/relative source paths, filenames, source
content, errors, caller values, and raw configuration.

The cursor advances only after all of these are true:

1. the managed source call returned;
2. the current receipt is reloaded read-only using the planned source content
   hash, version hash, and receipt configuration digest;
3. its state is `COMPLETE` and disposition is `WRITE`, `UNCHANGED`, or
   `ZERO_OUTPUT`;
4. `verify_receipt()` reports `represented` across drawers and closets;
5. one JSON record is appended with one bounded `O_APPEND` write and `fsync`.

If death occurs before step 5, the progress file lags by one source. Restart
replays that source through the normal managed idempotency path. It never
guesses forward.

## Restart and corruption rules

On restart, newline is the commit marker. Bytes after the last newline are a
torn append, so the reader truncates and fsyncs back to the last committed
record and replays that source. Every committed line must have valid UTF-8/
JSON, the exact schema, a contiguous index prefix starting at zero, matching
manifest item digests, and an intact record hash chain. Malformed,
digest-invalid, or divergent committed records fail closed.

Before skipping the prefix, the miner re-verifies every record against the
selected palace and re-hashes the current source under its source lock. The
manifest stat/content identity, HMAC source identity, current receipt UUID,
exact source hash/config, represented state, and represented count must still
agree. A completed source changed after its cursor was flushed is drift, and a
valid cursor copied to another palace cannot skip work.

`--start-index` is an assertion, not an override: it must equal the verified
prefix. Omitting it selects that prefix automatically.

## Lock contention

CLI project mining asks the programmatic miner to re-raise
`MineAlreadyRunning`. The CLI emits a path-free retry message and exits `75`
(`EX_TEMPFAIL`) instead of returning success. The legacy direct Python call
continues to print a warning and return, preserving its existing default
unless it opts into `raise_on_lock_conflict=True`.

## Verification

Focused tests cover:

- deterministic ordering and immutable manifest reuse;
- same-size/same-mtime source content drift;
- torn-tail recovery plus corrupt, divergent, skipped, and digest-tampered
  committed progress;
- no path, filename, or source-content leakage in progress;
- rejection before progress for non-terminal receipts;
- hard child-process exit after a flushed cursor and restart;
- distinct sanitized CLI lock-conflict exit;
- interrupted real disposable mining, exact-prefix resume, idempotent rerun,
  completed-prefix drift and cross-palace rejection, and equality with
  uninterrupted drawer/closet outputs.

The focused contract suite passes `10` tests. The expanded project-miner,
palace-lock, managed-receipt, CLI, and progress regression passes `223` tests
in `63.98s`; Ruff and the repository local-Markdown-link checker pass.

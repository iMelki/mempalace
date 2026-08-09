# Resumable source-plan contract

Date: 2026-07-29
Owner: [mempalace#25](https://github.com/iMelki/mempalace/issues/25)
Consumer: [agent-settings#486](https://github.com/iMelki/agent-settings/issues/486)

## Decision

Large project planning now has its own durable cursor. `mempalace mine
--dry-run --plan-out ... --plan-progress-jsonl ...` checkpoints directory
discovery and every completed file descriptor (normalized path, stat identity,
and content hash) before moving to the next item. After a process kill or host
restart, the same command validates the immutable operation identity, repairs
only a non-newline torn final append, and continues at the exact pending
directory/file cursor. A complete corrupt or divergent record fails closed.

The planner journal is separate from the source-execution progress journal.
Planning proves what belongs in the immutable batch; execution proves which
manifest sources have terminal palace receipts. Neither journal treats process
exit or console output as completion.

## Research and community evidence

- Windows normally buffers writes; Microsoft documents
  [`FlushFileBuffers`](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers)
  as the primitive that pushes buffered file data to the device. The journal
  flushes every framed record before it becomes a restart cursor.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html) obtains an
  exclusive lock before recovering an incomplete journal and distinguishes a
  committed boundary from partial state. The plan journal likewise has one
  owner, validates its hash chain in order, and truncates only an uncommitted
  torn tail.
- [Kubernetes Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
  warn that an application can be restarted after node loss and can even be
  started twice, so it must handle temporary files, locks, and incomplete
  output. The planner therefore makes replay deterministic and rejects a
  second concurrent owner instead of trusting single-process assumptions.
- A reported [Codex JSONL durability failure](https://github.com/openai/codex/issues/21196)
  describes state pointing at missing/unlinked rollout files and recommends
  lock, link, and post-flush verification. This supports binding cursor state
  to a visible, immutable journal path instead of relying on an open orphaned
  descriptor or an outer wrapper's memory.
- Community reports also show that unbounded JSONL state can make resume
  surfaces unusable ([openai/codex#25215](https://github.com/openai/codex/issues/25215)).
  The plan records are intentionally compact and contain no content bytes;
  outer operational histories retain totals plus a bounded recent window.

## Contract

- `--plan-progress-jsonl` is valid only with `--plan-out`.
- The journal identity binds the project root, wing, parser/config contract,
  excluded plan artifacts, and output plan identity.
- Each record is newline-framed, sequence-numbered, hash-chained, and fsynced.
- Directory listing is checkpointed before file hashing.
- Each file descriptor is checkpointed before the next file.
- Normal append is linear: validated in-memory state advances after fsync rather
  than replaying the full journal for every record.
- Restart replays and semantically validates the retained prefix.
- Only a final non-newline fragment can be truncated automatically.
- A complete corrupt line, identity drift, duplicate/conflicting discovery, or
  impossible cursor fails closed.
- The final manifest is still the immutable execution authority; the planning
  journal is restart evidence, not a substitute for manifest verification.

## Validation

`tests/test_mine_progress.py` includes:

- a forced crash on the second file proving that the first descriptor is not
  rehashed and restart continues at the exact next item;
- a torn-tail case proving that only uncommitted bytes are removed;
- the existing manifest, progress, drift, lock, receipt, and privacy cases.

All tests use disposable source roots and palaces. No configured palace or
vault is opened by this proof.

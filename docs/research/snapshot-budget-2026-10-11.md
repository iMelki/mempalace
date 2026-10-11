# Snapshot budget investigation, 2026-10-11

Historical S572 evidence below is retained. The S572b CTO policy decision and
local source follow-up are documented in
[nightly/weekly verification](nightly-weekly-snapshot-verification.md).

Owner: [MemSys #572](https://github.com/iMelki/memsys/issues/572).
Status: **PARTIAL**. No nightly timing fix or historical SQLite root cause is
proven. This draft records evidence and real corruption checks for CTO review.
The retained receipts,
logs and copied database were inspected locally. No live snapshot, lease change,
service change, scheduler change or runtime installation was performed.

## What the retained evidence proves

The last 15 retained run records span September 27 through October 11. They contain
one complete producer receipt, seven early maintenance-marker refusals, three
disk-admission refusals, one source-access refusal, two inner child timeouts and
one independently observed outer timeout. These are separate failure classes.

| Run UTC | Wrapper seconds | Observed disposition |
| --- | ---: | --- |
| 20261011T024529Z | 441.665 | Disk admission refused before child launch |
| 20261010T024529Z | 14358.995 | Child timeout at 13500.111 seconds |
| 20261009T024540Z | unknown | H466 outer timeout at 14405.41 seconds |
| 20261008T040749Z | 0.409 | Pre-existing maintenance marker |
| 20261007T024530Z | 14141.906 | Child timeout at 13500.164 seconds |
| 20261006T024653Z | 2772.125 | Access denied in source receipt metadata |
| 20261005T024515Z | 0.361 | Pre-existing maintenance marker |
| 20261004T024503Z | 0.322 | Pre-existing maintenance marker |
| 20261003T024504Z | 0.563 | Pre-existing maintenance marker |
| 20261002T081752Z | 0.469 | Pre-existing maintenance marker |
| 20261001T024505Z | 0.456 | Pre-existing maintenance marker |
| 20260930T024503Z | 0.276 | Pre-existing maintenance marker |
| 20260929T024710Z | 863.430 | Disk admission refused |
| 20260928T024538Z | 817.540 | Disk admission refused |
| 20260927T024515Z | 6177.092 | Producer receipt complete; not an independent restore |

The September 27 module finished in 5906.443 seconds. Its SQLite backup API phase
took 912.359 seconds. The remaining 4994.084 seconds include full integrity
verification, segment copying, metadata/count checks, hashing and final receipt
work; the old receipt does not split them. Its destination was flat for another
4985 seconds even though the run completed. A flat destination therefore cannot
identify an online-backup stall.

The October 7, 9 and 10 failures stopped before segment copying. Each retained
destination contains a 26,635,059,200-byte `chroma.sqlite3`. Source footprint was
28,893,871,433 bytes across 42,742 files. Source-footprint/pre-admission-reclaim
times were 83.248/240.804 seconds on October 7, 373.381/3556.890 on October 9,
and 53.811/256.084 on October 10. H466 measured October 9 child launch 6712.34
seconds into a 14400-second outer budget, leaving only 7687.66 seconds.

The September 27 SQLite file was 26,557,870,080 bytes: the later file is only
0.29% larger. Byte-copy volume barely increased. This does not rule out changes
in index/content complexity or storage contention.

Time from child launch to the retained SQLite last-write timestamp was
5143.168, 4050.792 and 5362.839 seconds respectively: metadata-derived effective
rates of 4.939, 6.271 and 4.737 MiB/s. These are **not** measured copy durations.
The worker could still be backing up, committing or checking integrity. Wrapper
samples identify the Windows virtual-environment launcher, whose CPU/I/O do not
describe the separate Python worker. [#569](https://github.com/iMelki/memsys/issues/569)
owns that observability gap. The historical SQLite operation and its external
storage/antivirus contention remain unproven.

October 11 never launched SQLite: free bytes were 114,033,385,472 against
129,825,602,890 required. The latest retained producer receipt is dated
September 27 04:25:26 UTC, well beyond the 48-hour freshness limit. Its reported
count comparison is empty; [MemPalace #40](https://github.com/iMelki/mempalace/issues/40)
owns that separate recovery-proof defect. Freshness, hashes, full integrity,
logical counts, offsite custody and restored queries are separate proofs.

## Backup method and upstream research

The fork uses SQLite's online backup API with 4096-page batches, followed by a
full `PRAGMA integrity_check`, catalog-scoped HNSW segment copies, source
`data_version` comparison and file hashes. It does not use raw file copying as
the production SQLite snapshot method. The lease refuses SQLite WAL/SHM/journal
sidecars. The read-only copy has 6,502,700 pages at 4096 bytes, 36,593 freelist
pages and DELETE journal mode. No historical reader/writer census exists.
Chroma's `embeddings_queue` WAL table is distinct from a SQLite `-wal` sidecar.

Research before the source proposal:

- [SQLite online backup](https://sqlite.org/backup.html) and
  [Python backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup):
  a complete backup is consistent; concurrent external commits can restart the
  operation. The offline copy test observed neither BUSY nor LOCKED callbacks.
- [SQLite cache size](https://sqlite.org/pragma.html#pragma_cache_size): the
  default suggested cache is 2000 KiB; a negative value specifies KiB, applies
  only to that connection and is allocated lazily.
- [SQLite full and quick integrity checks](https://sqlite.org/pragma.html#pragma_integrity_check)
  and [maintainer discussion](https://sqlite.org/forum/forumpost/f3ad6b1bb5):
  full table/index and uniqueness cross-checks are expensive. `quick_check`
  omits checks required here and cannot replace the full verification.
- [SQLite release history](https://sqlite.org/changes.html) and
  [FTS5 integrity documentation](https://sqlite.org/fts5.html#the_integrity_check_command):
  SQLite 3.44 added virtual-table integrity checks; 3.45.1 fixed their read-only
  use. The 3.45.3 runtime already contains that fix. A bounded FTS-only probe on
  the copied database reached content/docsize queries, but did not complete;
  it does not attribute the historical timeout to FTS5. A separate one-second
  probe classified the returned interruption row, avoiding a false corruption
  or success claim.
- [SQLite mmap](https://www.sqlite.org/mmap.html): mapping is a different
  optimization with platform limits and memory/security considerations. A
  preliminary experiment uses it; the source proposal leaves mmap unchanged.
- [Chroma backup guidance](https://cookbook.chromadb.dev/strategies/backup/) and
  [storage layout](https://cookbook.chromadb.dev/core/storage-layout/): SQLite
  metadata and UUID segment files jointly constitute the persistent database.
- [Chroma #7238](https://github.com/chroma-core/chroma/issues/7238) describes
  damaged HNSW segments despite passing SQLite integrity. It reinforces the
  separate segment/restore requirement; it is not this timeout's root cause.
  [MemPalace upstream #1362](https://github.com/MemPalace/mempalace/issues/1362)
  concerns SQLite index corruption before HNSW repair, not a snapshot speed fix.
- [Original MemPalace upstream](https://github.com/MemPalace/mempalace): searched
  issues/PRs, recent releases and changelog; upstream `develop` at
  `1f0803a31958b30320418356b2db88c4ed9c27f9` has no `backup_snapshot.py`.
  No applicable upstream snapshot fix was found. Chroma release changes did
  not establish a fix for this fork's full integrity reader; no upgrade proposed.

Related scope: [#653](https://github.com/iMelki/memsys/issues/653),
[#466](https://github.com/iMelki/memsys/issues/466),
[#423](https://github.com/iMelki/memsys/issues/423), and
[lease cleanup PR #869](https://github.com/iMelki/memsys/pull/869).
Lease recovery and Meili restore are independent from SQLite verification.
PR #869 was merged by its owner on October 11 at 17:12:55 UTC; this lane did
not install it or verify live adoption.

## Offline measurements and rejected proposal

All database measurements used retained failed staging copied into an isolated
lane on C:. The retained input and its copy SHA-256 matched:
`9670071505e9b4d8b8dd9bf6af280c71437b62be41ca7b1d21eae94fbf66863e`.
Raw database files and private operational paths are not published.

The existing MemPalace virtual-environment interpreter used for these copy
tests reports SQLite 3.45.3. On the copied input, the unchanged
online backup API completed all 6,502,700 pages in **268.407 seconds**
(94.63 MiB/s), with 1587 OK callbacks and one DONE callback. A raw retained-file
copy took 23.876 seconds. The completed API output SHA-256 also equals the input
SHA-256 above. Neither timing proves the historical storage speed.

The exact original helper and a prototype setting only the connection-local
cache target to 1 GiB were replayed serially on the same API output. Both used
SQLite 3.45.3, mmap size 0 and a 7200-second measurement deadline, with no
concurrent test suite. Both returned `ok` and reported 911,600,000 approximate
VM steps. The retained source and copies have normal archive attributes; no
compressed/encrypted flags or reparse-point ancestor were observed.

| Helper | Cache KiB | First FTS content/docsize signal (s) | FTS index signal (s) | Full verdict | Total (s) |
| --- | ---: | ---: | ---: | --- | ---: |
| Original | -2000 | 2064.001 | 2355.938 | ok | 2816.227 |
| Rejected prototype | -1048576 | 1765.852 | 2105.853 | ok | 2860.970 |

The larger-cache prototype was **44.743 seconds slower overall** in this pair,
while its sampled worker working set was about 1.28 GB versus 88 MB for the
original; sampled private memory was about 2.10 GB versus 876 MB. These are
observations, not peak measurements or a controlled causal estimate. Admission
physical RAM use differed (81.517% prototype, 86.359% original), and the original
ran after the prototype and a file-hash read. The prototype therefore supplies
no demonstrated total-time benefit that justifies its extra memory. It was
removed; the submitted runtime helper remains unchanged.

The original source-file SHA-256 is
`31aecbefb55aa85911d434c83c070658ea8910fb92a32f8eee85ef0bbe8496bb`.
The rejected prototype SHA-256 was
`351c88e38ec0f7a875b87c097dff5cd1deecccb5a51be3cb43d24de4675c8a14`.
The prototype's post-check database SHA-256 remained equal to the copied input.

Earlier bounded probes stopped without an integrity verdict: a 64 MiB reader
at 611.552 seconds, a different-interpreter default reader at 193.375 seconds,
and a preliminary cache+mmap reader at 3607.537 seconds. A shorter original-helper
replay stopped at 1803.568 seconds before FTS signals. None is a completion
time. A test suite overlapped part of the mmap probe; it is exploratory evidence.
The completed original replay demonstrates why those cutoffs cannot establish
a stalled or corrupt database. The historical timeout was not reproduced here.

Only the SQLite component was qualified at production size. The failed staging
input has no matching HNSW segment tree, and lane capacity does not support an
additional complete source and destination generation. Full production-shaped
snapshot completion, restored queries and nightly budget enforcement remain
unproven. Segments from another generation must not be substituted for them.

## Validation and handoff

The rejected prototype's cache-target test failed against the original helper
(one failed, two passed); it is not a retained timing-fix test. The submitted
characterization cases preserve the original read-only settings/database bytes,
reject malformed SQLite, reject index damage admitted by `quick_check`, and
accept intact/reject damaged FTS5 trigram content. The FTS cases need SQLite
3.45.1 or newer and execute on the measured 3.45.3 runtime. Final focused and
normal pre-push results are recorded with the draft PR's exact head. Final
focused snapshot checks passed **43 tests**, with **one skip**, in 16.93 seconds.
During prototype evaluation, the repository default selection passed **2142 tests**, with **9
skipped and 106 deselected**, in **1321.14 seconds**. The default markers exclude
benchmark, slow and stress tests. There were 257 warnings and no failures. Test
home and temporary files were isolated; no live memory corpus was tested.
The gate fixture constructs an otherwise valid empty index: `quick_check`
accepts it, while the actual snapshot verifier exits 1 with `row 1 missing
from index by_value`. Its restored input exits 0. The captures are recorded in
the [gate ledger](../../.gate-evidence.json).

Bounded utilization scan: the snapshot helper covers inline and deferred
verification. Independent readers in `evaluation_manifest.py`,
`receipt_restart_probe.py`, and the archive-restore rehearsal require their own
evidence. The rejected tuning was not promoted to any consumer or shared policy.

This draft has **no runtime installation step**. #572 stays open. The diagnosis
and runtime owners must first obtain actual-worker CPU/I/O and API, commit,
integrity, segment-copy and hash boundaries through #569's observe-only workflow.
Only then can a measured timing fix be selected, reviewed and adopted. Capacity,
marker ownership, remaining outer budget and #40's logical counts remain separate
gates. A complete generation needs its exact receipt, hashes and independent
restored-query proof before it is a backup to rely on.

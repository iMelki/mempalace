# Nightly quick check and weekly full verification

This fork-specific source candidate implements the CTO decision for the nightly
snapshot budget follow-up. It is local-only until public publication is approved;
no scheduled task, process, maintenance marker or live database was changed.

## Verification contract

Nightly snapshots keep SQLite online backup, writer-quiescence, catalog-scoped
segment copying, structural comparison (page count, freelist and schema hash),
logical-count comparison and file hashing. `PRAGMA quick_check` runs read-only on
the COPY and any non-ok, empty/multiple verdict or SQLite error refuses completion.
The receipt records `quickCheck=ok` and `integrityCheck=deferred-weekly` explicitly.
Deferred copy verification uses the same nightly policy.
Older pending manifests without captured structural metadata fail closed instead
of deriving missing source evidence from the current live database. The runtime
owner must disposition those generations separately during adoption.

Quick check deliberately does not prove UNIQUE constraints or index/table content
consistency. The existing empty-index fixture still passes quick check and fails
full integrity. Structurally corrupted index and table pages fail quick check.
The weekly production checker also backs the independent integrity contract tests.
The unused producer integrity helper is retired; evaluation-manifest checks retain
their separate policy.
The empty logical-count limitation remains a separate recovery-proof issue.

`python -m mempalace.backup_snapshot --weekly-verify-root <backup-root>
--palace-path <live-root> --weekly-budget-seconds 7200 --json` selects the newest
complete generation by receipt timestamp (pending copies are not candidates).
It resolves the configured backup root exactly as the nightly writer does, so
root junctions, volume mounts and cloud-backed ancestors are accepted. Within
each snapshot generation it refuses links and reparse points, including segment
files, and rejects hard links to either the supplied live root or receipt source,
and never opens a live SQLite connection or acquires a maintenance lease.

The weekly check validates the nightly receipt, verifies the copy's recorded hash,
runs full `PRAGMA integrity_check`, re-hashes the database and checks the original
receipt identity. Its own deadline covers hashing and SQLite VM work. Success or
failure is atomically saved to `weekly-integrity-receipt.json` beside the snapshot
receipt. The nightly receipt is preserved byte-for-byte. The weekly sidecar
records snapshot/database identities, SQLite version, UTC start/end, duration,
budget, input bytes, processed/error counts, caller and mutation intent.

Receipt readback reports weekly `missing`, `failed`, `invalid`, `stale` or
`complete` separately from nightly `valid`. Missing weekly evidence is observable;
it cannot masquerade as full integrity proof. New daily snapshots normally report
missing until selected by a weekly run. Weekly health must consume the latest
weekly receipt across generations and alert after seven days without success;
checking only each day's newest snapshot would alert continuously.

Both receipt-verification CLI entrypoints use one outcome classifier and renderer.
Exit 0 means nightly validity and complete, fresh weekly proof; exit 2 means
invalid nightly proof or failed/invalid weekly proof; exit 3 is a warning for
missing/stale weekly proof; exit 1 means the command could not evaluate its input.
Warnings are nonzero so callers cannot treat missing evidence as a clean result.
The JSON `valid` field continues to describe only nightly proof. A newly staged
generation normally warns until its weekly run completes; this command is not a
replacement for the separately gated cross-generation weekly health consumer.
Unknown weekly status values fail closed. SQLite error codes, when available,
distinguish progress-handler interruption from corruption near the deadline;
only a deadline-associated SQLITE_INTERRUPT is labeled budget exhaustion.

Runtime adoption is gated on consumer handling of warning exit 3. A bounded
source scan found an archive verifier that allows only exits 0/2 and requires 0
alongside nightly `valid=true`; it will reject a newly staged generation's missing
weekly proof under this contract. Its owner must explicitly classify warnings
before adopting this source, preserve nightly proof requirements, and still
reject failed/invalid weekly proof. No consumer policy was changed in this review.

## Research before implementation

- [SQLite quick_check](https://sqlite.org/pragma.html#pragma_quick_check): O(N),
  omits UNIQUE and table/index consistency checks.
- [SQLite integrity_check](https://sqlite.org/pragma.html#pragma_integrity_check):
  full integrity supplies the omitted cross-checks; neither validates HNSW.
- [SQLite 3.45.3 FTS5 source](https://github.com/sqlite/sqlite/blob/version-3.45.3/ext/fts5/fts5_main.c#L2785):
  its integrity method ignores the quick flag and runs the same storage check.
  Thus an FTS-heavy database may retain substantial verification time; only
  the copied-database measurement qualifies the expected saving here.
- [SQLite backup API](https://sqlite.org/backup.html) and
  [Python backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup):
  retain online backup; do not replace it with a live byte copy.
- [SQLite maintainer discussion](https://sqlite.org/forum/forumpost/f3ad6b1bb5):
  comparison work can dominate full verification.
- [Chroma backup guidance](https://cookbook.chromadb.dev/strategies/backup/):
  preserve the matching SQLite and HNSW generation.
- [Original upstream backup request](https://github.com/MemPalace/mempalace/issues/1120)
  remains open. [Upstream integrity PR](https://github.com/MemPalace/mempalace/pull/1962)
  uses quick check as a page-corruption gate in HNSW quarantine; it is open and
  does not implement this snapshot split.
- [Upstream contention PR](https://github.com/MemPalace/mempalace/pull/1932)
  is open: locked is inability to verify, not proof of corruption. This candidate
  refuses verification without labeling every SQLite error as detected damage.
- Inspected original upstream releases and
  [changelog](https://github.com/MemPalace/mempalace/blob/develop/CHANGELOG.md);
  no equivalent nightly/weekly snapshot job was found. No upstream code was ported.

## Separate live-install proposal (NOT applied)

Owner: runtime backup owner; source acceptance: acting CTO. Cadence: once weekly,
outside the nightly window after a fresh collision inventory. Register a NEW
windowless task only after source review and explicit runtime authorization.
Pin its executable/source identity; use the existing Job Object/native lifecycle
owner, closed stdin, durable stdout/stderr and an outer 7500-second ceiling around
the 7200-second inner job. The external bound is required for blocked OS I/O;
SQLite progress callbacks cannot interrupt an OS filesystem hang.

Sink: sidecar plus an outer job receipt and established backup health route.
Triage: owner inspects every nonzero/error/missing/stale receipt; a seven-day
absence of a successful weekly run is a finding even if nightly checks succeed.
Keep generations pinned during verification; do not let retention recycle them.
Run two attended copy-only pilots before registration, then verify exact task
readback, natural trigger, zero-active-descendant receipt and consumer acknowledgement.
No registrar, alert consumer or live task is installed by this source candidate.

## Native CLI protocol regression caught before publication

The first candidate imported the exception class from the producer module into
the new verifier. Running that producer with `python -m` created a second module
identity, so a weekly budget refusal escaped as a traceback with empty JSON
stdout. Imported-function tests did not expose it. The native subprocess case
failed first with a JSON decode error. A shared `snapshot_errors.py` now gives
both entrypoints the same exception identity; the native CLI emits its weekly
error schema on refusal and a saved weekly receipt on success. Both native cases
are regression-tested. This was fixed locally before publication or installation.

## Bounded utilization scan

The snapshot producer and deferred verifier share the new nightly primitive.
Evaluation-manifest full integrity, receipt restart probes and independent archive
restore checks retain their stricter policy. Existing snapshot/freshness consumers
use completion/proof flags, not `integrityCheck=ok`; their operator wording and
weekly-health integration need separately reviewed runtime-owner adoption.

## Measurement and validation

The same 26.64 GB copied SQLite file passed quick check in **2178.280 seconds**
with unchanged pre/post hashes, 6,502,700 pages and 36,593 freelist pages. Runtime
SQLite was 3.45.3; the post-check hash took 46.631 seconds. Previously measured
online backup was 268.407 seconds and full integrity was 2816.227 seconds. Small
contract suites overlapped this probe; no full suite did. Host load differed from
the earlier full-check run, so the time difference is not a controlled speedup.

The projection rule was written before the verdict: estimate other nightly work
as `5906.443 - 912.359 - 2816.227 = 2177.857` seconds from the prior producer and
copied full-check characterization, then add the measured API and quick check.
Expected nightly is **4624.544 seconds**, or **2.919x** headroom against the
13,500-second inner budget (criterion: <=6750 seconds for >=2x).

That residual is a cross-run estimate, not a measured phase or an upper bound.
The prior producer did not isolate full integrity and used a 0.29% smaller file.
Retaining ALL prior non-API work gives a conservative scenario of **7440.771
seconds**, **1.814x** headroom, below the inner budget but below the requested 2x
margin. Thus the nominal expected duration meets the target; a guaranteed or
observed whole-nightly 2x margin remains unproven without a matching full generation.

New verification logic is extracted into the 266-line verification module
(largest function 39 lines) and a five-line shared exception module. The existing
producer remains oversized; its lease/copy flow is preserved, with new small
wrappers and CLI dispatch. The unchanged large flows require CTO review rather
than an unrelated refactor in this policy change. The new test/fixture files stay
below their size targets.

The private lane report and proposed-write packet carry exact commands, timings,
hashes, full-suite counts and negative-proof captures. The production-size input
is the retained 26,635,059,200-byte COPY with SHA256
`9670071505e9b4d8b8dd9bf6af280c71437b62be41ca7b1d21eae94fbf66863e`.
A complete matching HNSW generation was unavailable. Therefore SQLite measurements
and a planning allowance are not an observed complete nightly run or restore.

## PR 79 review repair research (2026-10-12)

Original upstream was checked first: backup request #1120, absent-integrity-verdict
fix #2291, index-corruption report #1362, release v3.10.0 and the current develop
changelog. Bounded upstream searches for snapshot integrity returned no matching
issue; quick_check PR search found [repair retry #2628](https://github.com/MemPalace/mempalace/pull/2628).
That open repair change retries read-only refusals on a writable connection.
A snapshot verification must remain read-only, so that repair policy is not ported.
The absent-verdict principle in [#2291](https://github.com/MemPalace/mempalace/pull/2291)
already matches the strict all-rows checker used here; no equivalent root-boundary
or weekly receipt CLI fix was identified in this bounded review.

- [Microsoft reparse points](https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-points)
  explains why root mounts and junctions need a boundary rather than an all-ancestor refusal.
- [Python SQLite exceptions](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Error)
  documents SQLite error codes and names for distinguishing interrupted checks.
- [SQLite maintainer discussion](https://sqlite.org/forum/forumpost/f3ad6b1bb5)
  confirms that quick_check can miss cross-reference defects and full checks take time.

The production integrity contract now covers the exact single-ok result,
unchanged database bytes, read-only URI and default cache/mmap settings.
Future-clock replay sets both completed-generation timestamps explicitly and
keeps a later pending generation excluded. The live-copy refusal fixture now
passes the staged-root identity check and reaches the actual live-root guard.


Review-repair maintainability: verification module 230 -> 266 lines, largest
function still 39 lines. Producer 1265 -> 1257 lines and largest function 276;
CLI 1843 -> 1844 lines and largest function 557. Those pre-existing oversized
flows remain unchanged except the small receipt dispatch; this review removes
one dead helper and shares the outcome classifier/renderer instead of adding a
second policy. The review regression file is 162 lines, largest test 40 lines.
No scheduler, service, runtime-consumer or maintenance-lease adoption is claimed.


## Review repair validation and unresolved full-suite checks

Failing-first review regressions: 25 failed, 10 passed, 31 deselected. Corrected
focused snapshot suites: 104 passed, one unavailable cross-repository fixture
skipped. Production settings/verdict/link/live mutations caused 9 attributed
failures; all restored selections passed after byte-exact restore verification.
The production integrity fixture rejects a real empty index and accepts clean data.

Two complete CI-shaped coverage runs used the project virtual environment and
fresh temporary storage. First: 2202 passed, 1 failed, 9 skipped; coverage 83.90%.
Second: 2201 passed, 2 failed, 9 skipped; coverage 83.91%. Both exceed the 80%
coverage gate, and all snapshot regressions passed. The package suite is NOT green.
The unchanged fact-checker performance assertion exceeded 0.200s twice (0.201289s
and 0.216083s). The second run also hit permission denied while a project-scanner
fixture wrote a temporary Git object. Isolated diagnostics passed, but those
passes do not clear the whole-suite failures. No threshold, quarantine, skip,
ACL or unrelated module was changed. The acting CTO/test owners must resolve
these separately before package acceptance; public bug filing remains separately
gated. Review repairs are local commits only, with publication/runtime gates open.

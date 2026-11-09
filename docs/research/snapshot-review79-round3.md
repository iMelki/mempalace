# Snapshot review 79 round 3

This repair concerns copy isolation and saved verification outcomes. It does not
install a runtime, run a production backup, or change a scheduler or live lease.
The acceptance sources are native receipt CLI exits, isolated fixture readback,
focused snapshot tests and the package CI pytest selection with its 80% gate.

## Research before implementation

Original upstream was checked before designing the changes:

- [Backup request #1120](https://github.com/MemPalace/mempalace/issues/1120).
- [Backup proposal #448](https://github.com/MemPalace/mempalace/issues/448).
- [Read-only repair retry #2628](https://github.com/MemPalace/mempalace/pull/2628).
- [Release v3.10.0](https://github.com/MemPalace/mempalace/releases/tag/v3.10.0).
- [Develop changelog](https://github.com/MemPalace/mempalace/blob/develop/CHANGELOG.md).

Bounded issue searches for snapshot integrity, PR searches for quick_check and
file history for the snapshot module found no equivalent implementation to port.
The read-only repair retry opens a writable connection; that policy does not fit
copy-only verification, so no upstream code was adopted.

The design also consulted
[Python link counts](https://docs.python.org/3.13/library/os.html#os.stat_result),
[Python path replacement](https://docs.python.org/3.13/library/pathlib.html#pathlib.Path.replace),
[Python JSON](https://docs.python.org/3.13/library/json.html),
[Microsoft hard links and junctions](https://learn.microsoft.com/en-us/windows/win32/fileio/hard-links-and-junctions),
[SQLite quick_check](https://sqlite.org/pragma.html#pragma_quick_check), and the
[SQLite maintainer discussion](https://sqlite.org/forum/forumpost/f3ad6b1bb5).
Observed fixture behavior, rather than documentation alone, determines each
finding's disposition. Memory recall was degraded by backend timeouts and did
not establish candidate correctness.

## Findings and mechanisms

| Finding | Reproduction and repair |
| --- | --- |
| Readback accepts hard-linked or junctioned HNSW segments | Both native receipt CLI entrypoints returned 0 on real links. The shared `isolated_tree` guard now checks links and file link counts before inventory/hash reads, including cheap readback and relocated-source cases. |
| Root fallback writes inside live storage | Live root, metadata child and redirected root fixtures did not refuse. `outside_live` now rejects resolved destinations equal to or under the caller's live root before scanning or writing; the selected receipt's recorded root is checked before publication as well. |
| Deferred nightly verification opens a linked live catalog | A real linked SQLite fixture was certified complete. The deferred path guards the generation and every file before quick_check, counts or hashing. |
| Deferred receipt publication truncates an outside inode | Both success and corrupt-catalog failure publishers replaced the outside file's `keep` bytes. They now use the shared exclusive-temp-and-replace publisher and refuse existing linked receipts. |
| Read-only weekly sidecar retains accepted old success | Actual Windows read-only replacement failed; both linked-file and full-check failures left readback at exit 0. A saved failure now invalidates older positive proof by receipt identity and timestamp. A generation-specific invalidation record survives a later unrelated root error; a subsequent successful check supersedes it. |
| Deeply nested sibling escapes isolation | The stated 2,000-level example parsed on Python 3.13.2, so its precise recursion claim is refuted. At 20,000 levels the same parser raised an uncaught RecursionError, with and without a good sibling. The sibling boundary catches Exception, preserving scan errors and the good-copy or all-bad result. Process-control BaseException subclasses are not swallowed. |

Failing-first proof against `84486ff2980010334975e49151a151dd7f031e15`:
the initial 14-case run had 12 attributed safety failures and two assertions
whose expected recursion was not observed at depth 2,000. The corrected
20,000-level selection had two attributed RecursionError failures. The exact
2,000-level attempt is retained as a refuted detail, not a claimed reproduction.
Gate captures and restored checks belong in the root `.gate-evidence.json`.

## Boundary and receipt contract

The configured backup root may be a writer-supported junction. Redirects below
that boundary, including inside the staged palace and explicit staged-root
overrides, are refused before reading file contents. An independently copied,
relocated archive remains supported through its explicit staged-root override.
File independence uses `lstat().st_nlink` and applies even if the original source
has moved. It does not infer independence from equal bytes or a cached success.

The deferred JSON publisher creates a unique temporary file exclusively and
replaces the destination. It never opens the existing receipt for truncation.
A rejected precondition raises without touching an outside inode. Normal
verification failures still publish an error receipt in the safe generation.

When a weekly destination cannot be replaced, the safe backup-root error is
retained and a generation-specific invalidation is attempted. Receipt readback
checks both saved failure locations against the exact snapshot receipt identity
and the successful check timestamp. A newer matching error produces exit 2.
Failures to write invalidation are named explicitly in the root fallback.
If neither destination is writable, publication raises; no saved result is
claimed. Temporary files left after failed replacement carry no proof authority.

Declaring the backup root itself to be live storage now produces an immediate
no-write refusal. Two older fixtures were updated from expecting an error write
inside that declared live tree to asserting preserved bytes and no new receipt.

## Validation and limits

The complete CI-shaped selection passed locally on Python 3.13.2: **2,248
passed, 9 skipped**, exit 0, 1,473.10 seconds, **84.01% coverage** against the
80% CI gate. It included **149 focused snapshot passes and one existing
unavailable recovery-consumer skip**, including all 23 new round-three cases.
The earlier standalone changed-review/weekly selection passed 105 tests.
Source/test hashes were compared with the full-run input after completion and
remained unchanged. The final report carries raw-log pointers and exact commands.

Tests use fresh temporary roots, the project interpreter, deterministic native
fixtures and no production database. Real Windows junction and read-only cases
are conditional on Windows; hard-link and parser fixtures are portable. The
package selection excludes benchmarks and preserves existing marker policy.
The 80% CI coverage override is explicit; the project default is stricter.

The filesystem guard is a precondition check, not an atomic defense against a
concurrent hostile process replacing entries between a guard and a read. Backups
must remain under their existing exclusive owner during verification. Weekly
full integrity is a SQLite proof; it does not claim HNSW semantic integrity.
Runtime installation, provider adoption and natural scheduled delivery remain
outside this source repair and require the owning runtime's acceptance.

Maintainability: one shared link guard, tree traversal, live-root guard and
atomic JSON publisher replace duplicated policy at the three verification
consumers. The existing oversized producer retains its lease/copy flow. Only
its deferred verifier and receipt reader change; unrelated extraction is deferred
to a separately scoped review. New helpers target at most 50 lines and the new
regression file remains below the 700-line test target.

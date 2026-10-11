# Snapshot interruption tests and coverage measurement

Tracked by [PR77](https://github.com/iMelki/mempalace/pull/77).
The requested outcome is a local coverage repair at its exact head,
`92cd0b739fe630c2808ae2163c8057d6ca6b09ad`, with the unchanged CI gate
passing at 80% or higher and a normal local commit. Publication and hosted
validation remain separate, unperformed actions.

## Research before implementation

- Read the fork's CI workflow and coverage configuration. CI runs
  `python -m pytest tests/ -v --ignore=tests/benchmarks --cov=mempalace
  --cov-report=term-missing --cov-fail-under=80 --durations=10`.
  The package is the only measured source; test fixtures are already outside it.
- Read the original upstream first: [issues](https://github.com/MemPalace/mempalace/issues?q=snapshot+coverage),
  [PRs](https://github.com/MemPalace/mempalace/pulls?q=coverage+subprocess),
  [releases](https://github.com/MemPalace/mempalace/releases),
  [changelog](https://github.com/MemPalace/mempalace/blob/develop/CHANGELOG.md),
  and [coverage configuration](https://github.com/MemPalace/mempalace/blob/develop/pyproject.toml).
  The October 12 develop API head was `1f0803a31958b30320418356b2db88c4ed9c27f9`;
  its `mempalace/backup_snapshot.py` contents request returned 404. Bounded
  snapshot/subprocess searches returned no matching coverage repair to port.
  A local upstream reference at `48b262eec8cfa287101e5dfc137559184f133467`
  also contained no `settrace` use under tests. These are bounded observations,
  not a claim that all upstream history was searched.
- [Coverage.py's known problems](https://coverage.readthedocs.io/en/7.13.5/trouble.html)
  explicitly identify competing `sys.settrace` users as a measurement conflict.
  [Python's trace API](https://docs.python.org/3.13/library/sys.html#sys.settrace)
  documents the existing trace observer and automatic trace removal on errors.
- [pytest-cov subprocess support](https://pytest-cov.readthedocs.io/en/latest/subprocess-support.html)
  explains that version 7 removed automatic child measurement. This environment
  uses Python 3.13.2, pytest 9.0.3, pytest-cov 7.1.0 and Coverage.py 7.13.5.
  Subprocess support was considered, but the reproduced cause is interruption
  tests disabling the parent observer; no dependency or coverage configuration
  change is needed to repair it.

## Measured cause

The same CI command ran against pristine, isolated trees for the PR head and
`origin/dev` (`f79d625ce53629aa59c7eb3302d70841d310fd75`). Dev remained at that
SHA in GitHub's ref readback. Coverage JSON and `term-missing` reports establish:

| Candidate | Executable lines | Measured lines | Missing lines | Coverage |
| --- | ---: | ---: | ---: | ---: |
| Dev | 18,604 | 15,561 | 3,043 | 83.64% |
| PR77 before repair | 18,719 | 14,785 | 3,934 | 78.98% |
| PR77 after repair | 18,719 | 15,671 | 3,048 | 83.72% |

The PR adds only 115 net executable statements. It cannot explain the extra
891 missing statements solely through new source. Unchanged modules lose
measurement after the snapshot interruption tests: `sweeper.py` loses 327
previously measured lines; `write_receipts.py` loses 158;
`split_mega_files.py` loses 84; `spellcheck.py` loses 77.

Both interruption tests replace the current observer with a one-shot failure
injector, then clear tracing in their `finally`. A standalone invocation of
the actual test with an existing observer failed with
`lease interrupt probe discarded the existing trace observer`.
The same mistake exists in two standalone lifecycle-probe modes.

The added executable lines missing coverage at the original PR head are:

| Source file | Missing added lines before | Missing added lines after |
| --- | --- | --- |
| `backup_snapshot.py` | 351, 352 | 351, 352 |
| `maintenance_identity.py` | 35, 71 | 35, 71 |
| `snapshot_lease_release.py` | 56, 76, 90-93, 95-96, 110, 113, 120-122 | 76, 90-93, 95-96, 113, 120-122 |

These 17 lines include Windows error paths and explicit POSIX compatibility.
No production line is newly excluded, and no pragma is added.

## Repair and negative proof

Capture `sys.gettrace()` before borrowing the observer and restore that exact
object in `finally`, including after a tracing callback raises. Apply this to
both normal tests and both standalone lifecycle-probe modes. Six new cases
assert restoration and observation of a later real `sha256_file` call, across
post-acquisition and return-boundary interruptions.

The committed `tests/fixtures/snapshot_trace_custody_probe.py` reproduces the
mistake by replacing exactly two restoration sites in an in-memory test copy;
it asserts that the break applied. Its broken invocation exits 1 with the
observer-custody assertion. The restored invocation exits 0 and reports six
successful cases. Scratch markers are retained and are never live leases.
The ledger records the exact commands, attributed failure and restored pass.

The bounded utilization scan found the same mistake only in the two test
functions and two standalone probe modes in this repository. They are all
repaired, and the inherited testing instructions now require observer custody.
Broader coverage/process changes are outside this repair.

## Validation and limits

The pristine PR baseline had 2,163 passes, 11 skips and one unrelated timing
failure: `test_performance_bounded_by_mentioned_names` took 0.236 seconds
against its 0.2-second assertion. Its full run took 918.80 seconds.
Dev had 2,137 passes, 9 skips and zero failures in 849.64 seconds.
Those baseline runs overlapped; the final repaired full suite runs alone.
The timing assertion and production entity-filtering code remain unchanged.

The final run passed the unchanged CI command at 83.72%: 2,170 passed,
11 skipped, zero failures and 525 warnings in 990.61 seconds. Measurement
recovers 886 executable lines; measured source remains 18,719 statements and
excluded source remains 358 lines. All six observer regressions pass. Both
standalone probe modes also pass with an existing observer installed. Ruff
lint and formatting pass (165 files), as do local Markdown links and staged
whitespace checks. No test/source file changed during this final run.

The main test file grows from 236 to 270 lines, with its largest function
43 lines. The new negative fixture is 53 lines, with a 40-line main function.
The existing lifecycle fixture needs only the same two capture/restore edits;
its historical layout is preserved. No production extraction is needed.

The shared virtual environment supplies dependencies, while pytest's existing
`pythonpath = ["."]` selects source from each isolated tree. Fresh TEMP, TMP
and TMPDIR folders are used for every run. Admission samples measured 89.80%
RAM and 79.81% commit usage before baselines, then 79.39% RAM and 75.47%
commit usage before the final suite, below the delegated 92% limits.

No public write, live lease, store, service, schedule or runtime selection is
changed. Linux/macOS and hosted CI require later authorized publication.

# Historical cohort recovery plan for issue #22

Date: 2026-07-14

Status: **NO-GO until every gate is fresh and the operator explicitly approves a named live scope**

## What the past evidence actually says

The 18 records are 18 intact historical conversation-export source versions.
They are not 18 drawers, vectors, confirmed old writes, confirmed deletions, or
confirmed corruption events. The July 12 read-only audit partitioned 29,449
unique staged sources into 25,448 exactly represented, 1,435 inventory-only,
2,548 excluded by current format rules, and these 18 candidates.

All 18 still matched their retained source hashes and produced 22,220 projected
rows under the audited current rules. None had an exact current representation,
and neither selected post-attempt snapshot contained an exact source link. All
18 exceeded the former 10 MiB scan cap, so a historical silent skip is a
credible common cause. The exact old runtime version was not retained, which is
why the classification remains `probable-never-receipted-current-rule-output`
rather than “proven skipped” or “proven lost.”

W3C PROV distinguishes a source entity from the activity that generated an
output, and OpenLineage records concrete run/input/output events. Reconstructing
the source and a current projection does not create evidence that an old write
activity happened. A future replay must therefore emit a new, honestly dated
`historical-recovery` receipt; it must never impersonate the missing historical
write-time receipt.

## Bounded cohort

| Order | Candidate | Provider | Format | Source MiB | Projected rows | Lane |
|---:|---|---|---|---:|---:|---|
| 1 | H02-61d6f2a4 | Codex | JSONL | 15.04 | 102 | live canary |
| 2 | H12-aa19398c | Codex | JSONL | 87.90 | 33 | attended standard |
| 3 | H05-0ec08720 | Codex | JSONL | 45.68 | 39 | attended standard |
| 4 | H03-bda2192a | Gemini CLI | JSONL | 25.59 | 99 | attended standard |
| 5 | H13-5a3c2999 | Codex | JSONL | 157.39 | 105 | attended standard |
| 6 | H04-c7d18db2 | Codex | JSONL | 103.73 | 125 | attended standard |
| 7 | H01-e5b90085 | Codex | JSONL | 12.66 | 205 | attended standard |
| 8 | H08-e44c9165 | Codex | JSONL | 18.51 | 283 | attended standard |
| 9 | H09-bdfbf31f | Codex | JSONL | 18.09 | 288 | attended standard |
| 10 | H06-6dfdaa77 | Codex | JSONL | 20.43 | 410 | attended standard |
| 11 | H15-e3b1723c | Copilot | JSONL | 10.04 | 635 | attended standard |
| 12 | H18-ee62efb8 | Codex | JSONL | 60.09 | 803 | attended standard |
| 13 | H17-9c28e553 | Copilot | JSONL | 16.38 | 905 | attended standard |
| 14 | H14-ee9d16f4 | Copilot | JSONL | 17.31 | 1,097 | attended standard |
| 15 | H11-137735c6 | Codex | JSONL | 135.81 | 2,034 | attended standard |
| 16 | H10-11c63a5d | ChatGPT | JSON | 13.40 | 3,823 | high output, last |
| 17 | H07-79400433 | ChatGPT | JSON | 10.39 | 5,406 | high output, last |
| 18 | H16-a12962bb | ChatGPT | JSON | 10.64 | 5,828 | high output, last |

The three ChatGPT candidates account for 15,057 projected rows, about 68% of
the cohort. They stay last and run individually. The order after the canary is
a proposed attended order, not an unattended queue: every checkpoint may
change the next choice based on fresh evidence and resource telemetry.

## Required gates

1. Rerun the same privacy-safe read-only audit immediately before recovery.
   Require the same 18 source-version hashes, no symlinks, no new exact
   representation, and the same projection implementation identity.
2. Compare normalized content fingerprints against existing rows. Exact source
   IDs being absent does not rule out equivalent content under unrelated legacy
   IDs; every equivalence needs a reviewed keep/reuse/replay disposition.
3. Cleanly stop every palace client and prove no second `PersistentClient` is
   open. Archive the entire palace directory, not SQLite alone, with per-file
   and archive hashes plus a backup run receipt.
4. Restore that archive into a disposable directory using one client. Require
   SQLite integrity, exact collection/count parity, receipt-journal presence,
   baseline vector retrieval, and clean close/reopen. The ten current non-zero
   archives and the older SQLite-header extraction drill do not yet prove this.
5. In the clone, ingest one candidate through the managed receipt path. Require
   exact expected IDs, no missing/excess/conflict results, idempotent rerun,
   retrieval, restart persistence, and rollback to the baseline clone.
6. Obtain explicit live approval naming the candidate scope, downtime window,
   H02 canary, and pre-authorized rollback. Approval of a plan or canary does
   not silently approve all 18.
7. Run one source per attended execution. After each source, verify its exact
   receipt, retrieval, restart persistence, unrelated-row drift of zero, host
   pressure, and a human/agent continue decision.
8. Close only the bounded cohort after all 18 source versions and 22,220
   expected outputs reconcile. Do not convert that result into a claim of
   global all-history MemPalace completeness.

Stop immediately on source/audit drift, unresolved equivalent content, stale
backup evidence, non-admitted host pressure, any receipt conflict, unexpected
collection or unrelated-row change, retrieval failure, or restart-persistence
failure. Rollback restores the proven full-directory snapshot and repeats the
same integrity, receipt, retrieval, and reopen checks before service resumes.

## Evidence

- Machine-readable plan:
  `docs/research/historical-cohort-recovery-plan-2026-07-14.json`
- Privacy-redacted audit:
  `%LOCALAPPDATA%\MemSys\eval-artifacts\mempalace-historical-provenance\mempalace-historical-provenance-20260712T035203003988Z.json`
- Audit receipt:
  `audit-result-8530c7e5f405c7a3d68da1735433543a2ef05f392dcdfae63fc14c74be894d4c`
- Audit SHA-256:
  `8e1abb45a63945045279ed73ff191052d16eb2077a8be7a4d0ce8be2e9802a75`
- [W3C PROV-DM](https://www.w3.org/TR/prov-dm/)
- [OpenLineage run lifecycle](https://openlineage.io/docs/spec/run-cycle/)
- [SQLite Online Backup API](https://www.sqlite.org/backup.html)
- [Chroma low-volume HNSW durability issue #6975](https://github.com/chroma-core/chroma/issues/6975)
- [Chroma same-directory second-client issue #7040](https://github.com/chroma-core/chroma/issues/7040)

No source replay, palace mutation, Railway access, or historical receipt
fabrication occurred while producing this plan.

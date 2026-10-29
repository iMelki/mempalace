# mempalace: usable-release plan and audit ledger

Dated 23 October 2026. This is an actionable reconciliation of the existing audit, not a new product roadmap or a claim that every item was fully assessed. It completes the plan and ledger specification while keeping missing evidence explicit.

## Authority and existing plans

Use [ROADMAP.md](../../ROADMAP.md) and the [existing review handoff](2026-10-22-review-handoff.md). Those existing product and operating contracts retain authority. GitHub issues remain the task source of truth; the [companion ledger](fleet-assessment-ledger-2026-10-23.json) is a dated evidence/disposition snapshot, not another maintained backlog. It records exact recovered item IDs and explicit unknown populations rather than inventing absent rows.

Existing source, publication and runtime owners retain custody. This document authorizes no repair, installation, service change, provider operation or release.

## Assessment boundary

Historical inventory: 75 records, observed non-atomically in retained 22 October evidence. It is not a fresh census or a completion percentage. The ledger contains 6 exact selected/recovered rows with evidence limits; remaining original membership and prose are explicit recovery requests.

Coverage and provenance are retained in the ledger. Missing historical assessment evidence does not prevent specifying useful releases; it prevents claiming full item assessment. Source, build, publication, selected installation, runtime acceptance and customer benefit remain separate facts.

## First usable release and ordered next releases

No new semantic version is invented. U0 qualifies or completes an existing useful baseline; U1 and U2 build on it by value and dependency.

### U0 — Qualify the existing fork as a recoverable recall backend

**Intended consumer:** memory coordinator and the operator using an already selected project memory corpus.

**Useful outcome:** Retrieve a known project fact through the existing backend and recover that same useful query from a verified snapshot.

**Existing baseline and its limits:** Existing fork at historical dev f672b37f31e9ce3812f25b5363570eee4f352790. Merged #77/#78 source is retained evidence; installed qualification is unknown.

**Minimum scope:**

- Keep the existing storage/API contract and upstream compatibility.
- Bind selected writer/reader identity, snapshot content and an independent restore.
- Preserve owned-object release and foreign-replacement safety from existing snapshot lanes.

**Blockers and dependencies:** mempalace#67, mempalace#77, mempalace#79, mempalace#82, restricted memory-coordinator dependency. These references are retained historical context, not freshly verified current states.

**Completion criteria and tests:**

- Known-result query succeeds on the selected candidate and disposable restored corpus.
- No-hit/unavailable cases remain truthful; counts/integrity/content identities are checked.
- Catchable and forced exits preserve the declared owned/stale identity contract; foreign replacement survives.

**Source, build, selection and rollback:** Existing fork maintainer produces an exact reviewed artifact; runtime custodian selects it only after backup/rollback plan and separate approval.

**Owner or acceptance gap:** Existing MemPalace/memory coordinator integration and backup custodians; acceptance gaps remain.

**Execution state:** planned only; no source/build/install/runtime result is asserted by this document.

### U1 — Resume interrupted ingestion without wrong lineage

**Intended consumer:** An explicitly authorized memory-ingestion owner.

**Useful outcome:** Resume a bounded interrupted mine without accepting unrelated heads or repeating already completed source work.

**Minimum scope:**

- Reuse merged #78 and existing source-progress/receipt contracts.
- Cover hard exits before checkpoint and intervening writers.
- Use existing guarded reader-close/admission boundaries.

**Blockers and dependencies:** U0, mempalace#69, mempalace#70, mempalace#71, mempalace#75, mempalace#78, restricted memory-coordinator dependency. These references are retained historical context, not freshly verified current states.

**Completion criteria and tests:**

- Failure-first crash/interleaving fixtures reject unrelated receipt lineage.
- Recovery remains correct after a second crash and metadata-preserving source revert.
- No duplicate-ingestion claim is inferred from structurally valid metadata alone.

**Source, build, selection and rollback:** Existing source lane and ordinary targeted tests; actual mining remains a separate explicit operation.

**Owner or acceptance gap:** Existing mining source/runtime custodians.

**Execution state:** planned only; no source/build/install/runtime result is asserted by this document.

### U2 — Provide dependable unattended backup and lifecycle evidence

**Intended consumer:** Existing backup operators and memory coordinator health consumers.

**Useful outcome:** A natural scheduled backup finishes with truthful lifecycle evidence and a useful, generation-bound recovery path.

**Minimum scope:**

- Existing #73/#74/#76 lifecycle and #40/#79/#82 snapshot lanes.
- Bind local restore before any offsite acceptance.
- Qualify portable runner instructions without duplicating upstream roadmap.

**Blockers and dependencies:** U1, mempalace#73, mempalace#74, mempalace#76, mempalace#40, restricted memory-coordinator dependency, restricted memory-coordinator dependency, restricted memory-coordinator dependency. These references are retained historical context, not freshly verified current states.

**Completion criteria and tests:**

- Mandatory receipt failure remains red while owned cleanup and subsequent setup proceed.
- Natural-run snapshot, restored application query and any offsite copy share exact generation/content identity.
- Declared cadence and failure telemetry are observed; no source test is called live adoption.

**Source, build, selection and rollback:** Existing publication/runtime owners, exact task definition and rollback readback under separate approval.

**Owner or acceptance gap:** Existing lifecycle publisher and backup/recovery custodians.

**Execution state:** planned only; no source/build/install/runtime result is asserted by this document.

## Prioritized audit and source tasks

1. Recover the missing original evidence listed below, without repeating completed readings.
2. Reconcile exact recovered rows and existing source supersession before selecting U0 work.
3. Confirm the existing owner or retain a named acceptance gap; do not create a duplicate source lane.
4. Integrate this plan and ledger through the existing publisher. Publication and consumer pickup require actual saved-state readback.
5. Execute U0 only under its separate source/runtime authorities, then advance U1 and U2 when their declared criteria are met.

## Missing-source requests

- **mempalace-evidence-1:** Original75 semantic rows and exact discussion counts. Owner: Existing original audit custodian and fleet coordinator. Recover the existing exact packet, source membership or explicit scope exception; do not repeat completed issue/source reads. Completion: A retained artifact is joined by identity and provenance, or a named unrecoverable/inaccessible exception is accepted.
- **mempalace-evidence-2:** Complete source manifest. Owner: Existing original audit custodian and fleet coordinator. Recover the existing exact packet, source membership or explicit scope exception; do not repeat completed issue/source reads. Completion: A retained artifact is joined by identity and provenance, or a named unrecoverable/inaccessible exception is accepted.
- **mempalace-evidence-3:** 57/62 inaccessible unknowns. Owner: Existing original audit custodian and fleet coordinator. Recover the existing exact packet, source membership or explicit scope exception; do not repeat completed issue/source reads. Completion: A retained artifact is joined by identity and provenance, or a named unrecoverable/inaccessible exception is accepted.

## Audit-plan completion rule

The plan/ledger is complete as an actionable document when every proposed release has a consumer, outcome, minimum scope, blockers/dependencies, tests, artifact/selection/rollback approach and owner or explicit gap, and every recovered item has an honest disposition/action. Unknown source populations must stay explicit and requested. Full underlying assessment, publication, repair completion and live adoption are separate outcomes and remain open where evidence is missing.

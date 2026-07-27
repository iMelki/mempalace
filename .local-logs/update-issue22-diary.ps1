$ErrorActionPreference = 'Stop'

$issue = gh issue view 22 --repo iMelki/mempalace --json body | ConvertFrom-Json
$body = [string]$issue.body
$body = $body -replace "`r`n", "`n"

function Assert-Contains([string]$Text, [string]$Needle) {
    if (-not $Text.Contains($Needle)) {
        throw "Expected issue-body marker was not found: $Needle"
    }
}

Assert-Contains $body 'Commit `3cd8d10` additionally routes MCP drawer add/update/delete and diary writes through that managed lifecycle.'
$body = $body.Replace(
    'Commit `3cd8d10` additionally routes MCP drawer add/update/delete and diary writes through that managed lifecycle.',
    'Commit `3cd8d10` routes MCP drawer add/update/delete and diary writes through that managed lifecycle. Commit `38a18e3` adds receipt-managed whole-file diary ingestion for deterministic day drawers and their complete derived closets.'
)

Assert-Contains $body 'Commit `f303e8b` on `dev` adds the process-restart proof and two machine-readable governance contracts. Commit `3cd8d10` adds the managed MCP mutation tranche without touching production data.'
$body = $body.Replace(
    'Commit `f303e8b` on `dev` adds the process-restart proof and two machine-readable governance contracts. Commit `3cd8d10` adds the managed MCP mutation tranche without touching production data.',
    'Commit `f303e8b` on `dev` adds the process-restart proof and two machine-readable governance contracts. Commit `3cd8d10` adds the managed MCP mutation tranche. Commit `38a18e3` adds the managed diary-file tranche. None of these tranches touched production data.'
)

Assert-Contains $body '- [ ] Diary ingestion.'
$body = $body.Replace('- [ ] Diary ingestion.', '- [x] Diary ingestion.')

$diarySection = @'
### 2026-07-15 managed diary-file tranche

- One dated Markdown file is one managed source. A changed source atomically replaces its deterministic day drawer and the complete current set of derived closets under one receipt and durable rollback snapshot.
- Exact source bytes are the source identity. The exact known-entity and entity-language snapshot participates in the run/config digest and drives both drawer metadata and closet extraction. `force` rewrites without falsifying that identity.
- Duplicate files for the same `(wing, date)` fail before palace mutation. Undated-only Markdown is a no-op before palace creation. A semantic zero-output update removes stale managed rows and publishes a zero-output successor receipt.
- Missing or renamed files are deliberately not treated as deletion authority. Historical deletion/recovery still requires a separate attended decision.
- The local diary state file is an atomic, cross-process-locked convenience cache, not authority. Malformed entries and non-object roots are repaired without poisoning receipt-backed retries.
- Exact vector snapshots and unchanged-source reuse retry only a classified transient Chroma visibility error for at most two seconds, re-reading the full row set each time. Unrelated or persistent failures remain fail closed.
- Independent review found four material gaps: entity configuration missing from receipt identity, scalar state poisoning, inconsistent entity-language use, and non-object state poisoning. All four were fixed and re-reviewed.
- Focused diary proof: `13` passed. Expanded managed-writer proof: `207` passed. Final repository/pre-push gate: `1,706` passed, `7` skipped, `106` intentionally deselected. Ruff, format, Markdown-link, JSON, diff, and secret checks passed.

'@

Assert-Contains $body '## What Past Writes Mean'
$body = $body.Replace('## What Past Writes Mean', $diarySection + '## What Past Writes Mean')

Assert-Contains $body '- [x] Route MCP drawer add/update/delete and diary writes through managed source receipts with exact semantic verification.'
$body = $body.Replace(
    '- [x] Route MCP drawer add/update/delete and diary writes through managed source receipts with exact semantic verification.',
    "- [x] Route MCP drawer add/update/delete and diary writes through managed source receipts with exact semantic verification.`n- [x] Route diary-file drawer/closet ingestion through whole-source managed receipts with exact byte/config identity and fail-closed vector snapshots."
)

Assert-Contains $body '- Commits `f303e8b` and `3cd8d10` on `dev`.'
$body = $body.Replace(
    '- Commits `f303e8b` and `3cd8d10` on `dev`.',
    ('- Commits `f303e8b`, `3cd8d10`, and `38a18e3` on `dev`.' + "`n" + '- `docs/research/diary-file-managed-write-contract-2026-07-15.md`.')
)

$body | gh issue edit 22 --repo iMelki/mempalace --body-file -
if ($LASTEXITCODE -ne 0) {
    throw "gh issue edit failed with exit code $LASTEXITCODE"
}

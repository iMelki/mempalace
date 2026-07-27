## Summary

Promote the reviewed MemPalace native loopback Streamable HTTP transport and managed write-receipt foundation from `dev` to `main`.

This removes supergateway from the steady-state local HTTP MCP path while retaining stdio and an explicit rollback route. It also establishes V1 managed-write receipts and provenance foundations for future recovery work.

## What Changed

- Added native authenticated loopback Streamable HTTP MCP transport.
- Extracted transport-neutral dispatch and bounded backend concurrency.
- Added Origin/auth/session/cancellation/cleanup tests.
- Added managed write receipts, verification, provenance, and source-context foundations.
- Updated miners/backends to participate in the managed-write contract.
- Pinned MCP SDK `1.28.1` at the private session-manager boundary.
- Repaired package metadata punctuation to ASCII after independent review caught double-encoded text.

## Validation

- Full suite: `1,673 passed, 7 skipped, 106 deselected` in `134.86s`.
- Independent focused review: `281 passed, 1 skipped` in `58.18s`.
- Ruff passed across changed source and tests.
- `git diff --check main..dev` passed.
- Attended four-client authenticated MCP smoke passed `4/4` workers and cleanup.
- Sustained burn-in passed six waves x four clients: `24/24` calls, `24/24` cleanup, zero lingering workers, stable bridge identity, `132.39s`.
- Fresh transport decision: `native-transport-ready`.

## Evidence

- Burn-in: `C:\Users\Milky\AppData\Local\MemSys\eval-artifacts\bridge-concurrent-mcp\mempalace-attended-native-sustained-burnin-20260714T060927Z.json`
- Readiness: `C:\Users\Milky\AppData\Local\MemSys\eval-artifacts\bridge-readiness\memsys-bridge-readiness-20260714T061336Z.json`
- Transport decision: `C:\Users\Milky\AppData\Local\MemSys\eval-artifacts\bridge-transport-readiness\mempalace-bridge-transport-readiness-latest.json`

## Boundaries

- Local-only; no Railway or hosted deployment.
- Default backend concurrency remains `1`; higher values stay opt-in.
- Disposable real-Chroma interruption/restart recovery and receipt durability history remain under MemPalace #22.
- The elevated scheduled-task execution boundary remains separate under agent-settings #300.

Closes #21 after merge.
Related: iMelki/agent-settings#209, #300; #22.

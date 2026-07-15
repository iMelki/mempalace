# Receipt-required core miner contract

Date: 2026-07-15
Owner: MemPalace issue #22
Status: implemented on `dev`; no configured or live palace was opened

## Decision

Project and conversation miner helpers may no longer perform a non-dry write
when their `ReceiptStore` or `ManagedRunIdentity` is absent, invalid, or belongs
to another receipt root. They fail before collection reads, purges, or upserts.
Receipt-free execution remains available only for dry runs.

The top-level `mine()` and `mine_convos()` paths already create the required
store and run, so ordinary CLI use retains its managed behavior. Low-level
callers that invoked `process_file()` or `_process_conversation_file()`
directly must now either use the top-level API or create an explicit run for
the target palace.

## Why this exists

The old optional parameters allowed the canonical source writers to fall back
to best-effort purge and upsert behavior. That fallback could mutate drawers or
closets without a source version, exact output manifest, durable predecessor
snapshot, terminal event, or rollback proof. It therefore made a global
provenance claim impossible even though the normal CLI path was managed.

The contract follows three existing community principles:

- [OpenLineage's run cycle](https://openlineage.io/docs/1.44.0/spec/run-cycle/)
  associates input and output datasets with a run and records a terminal
  `COMPLETE`, `ABORT`, or `FAIL` state.
- [W3C PROV-DM](https://www.w3.org/TR/prov-dm/Overview.html) models a derived
  output through the activity that used its input and generated it.
- [OWASP fail-secure guidance](https://owasp.org/www-community/Fail_securely)
  recommends denying an operation when its governing control is missing or
  fails, rather than silently allowing the less protected path.

This is an integrity and observability boundary, not a claim that source-write
receipts are a security sandbox.

## Runtime contract

| Invocation | Result |
|---|---|
| Top-level non-dry project or conversation mine | Creates one store/run and writes through managed receipts |
| Low-level non-dry helper with valid same-root store/run | Allowed |
| Low-level non-dry helper with either value missing or of the wrong type | Fails before collection mutation |
| Low-level non-dry helper with a run from another receipt root | Fails before collection mutation |
| Dry run without receipt objects | Preserved |

`ManagedRunIdentity` carries an in-memory private receipt-root binding. It is
not included in `as_dict()` or receipt events. `ReceiptStore.begin_source()`
also enforces the binding, so callers cannot bypass the helper guard by using a
foreign run directly.

The batched mining benchmark now creates a disposable managed run. Its explicit
historical unbatched baseline remains benchmark-only code and is not a canonical
miner fallback.

## Verification

- Focused requirement and real-Chroma batching proof: `5` passed in `1.31s`.
- The real-Chroma proof retained bounded write batches of `2`, `2`, and `1`.
- Final expanded receipt/miner/conversation/CLI proof: `209` passed with `29`
  dependency warnings in `69.91s`, with no stderr. Durable local evidence is
  `receipt-required-expanded-final-20260715T053201.{out,err}.log`.
- Final repository gate: `1,735` passed, `7` skipped, `106` intentionally
  deselected, and `220` dependency warnings in `211.25s`, with no stderr.
  Durable local evidence is
  `pytest-receipt-required-full-final-20260715T054246.{out,err}.log`.
- Ruff, manifest JSON parsing, and `git diff --check` passed after the final
  documentation update.

All tests use disposable paths and deterministic or mocked embedding behavior.
No configured palace, historical cohort, hosted service, or Railway resource
was read or mutated.

## What this does not finish

This retires one of the six unmanaged mutation surfaces in the issue #22
manifest. It does not receipt-manage AAAK compression, LLM closet
regeneration, migration, repair, deduplication, backend-open maintenance,
public direct collection handles, unmanaged `PalaceContext`, the legacy repair
implementation, or the MCP transport cache.

Independent review recommends AAAK compression as the next derived-write
tranche. Its successor must use an isolated derived lane, bind the exact
represented upstream receipt and deterministic Dialect/config identity, avoid
copying `write_*` metadata, preserve the upstream receipt before and after the
projection, and use durable rollback plus exact terminal verification. LLM
regeneration remains a separate, more destructive and externally
nondeterministic successor design.

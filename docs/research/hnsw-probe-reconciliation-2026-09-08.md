# HNSW probe integration review

Tracking: [#51](https://github.com/iMelki/mempalace/issues/51).

Reconciled unique local probe memoization, timeout/evidence budgets, and degraded
receipts with dev `331b2714`. Kept upstream native lifecycle closure and HTTP port
18787. Review also caught an API regression: additive receipt parameters had
displaced the eighth positional `candidate_strategy` argument. The integrated
signature retains that argument and a regression test exercises its validation.

The probe key is file-stat identity, not a cryptographic content address.
Historical live measurements in the changelog were retained as dated evidence;
this integration did not reproduce them or restart a service.

Offline validation used deterministic embeddings, temporary palaces and offline
model flags: 211 passed, 1 skipped, 78 warnings in 121.79 seconds. The invocation
did not print the skip reason; skipped coverage is unverified. After the API
repair, 31 tests passed with 29 warnings in 13.45 seconds. Ruff lint/format passed.
The real confirmed-divergence handler test failed for the intended reason after
an asserted one-site predicate break, then passed after exact-hash restoration.
The receipt is recorded in `.gate-evidence.json`.

Independent reviewer `review_mempalace` accepted a bounded cohesion exception:

| Source/test file | Nonblank source lines before/after | Largest function after |
| --- | --- | --- |
| `mempalace/backends/chroma.py` | 1304 / 1354 | 109 physical lines, unchanged |
| `mempalace/mcp_server.py` | 2141 / 2349 | 99 physical lines |
| `mempalace/searcher.py` | 933 / 963 | 290 physical lines |
| `tests/test_hnsw_capacity.py` | 571 / 658 | 87 physical lines, unchanged |
| `tests/test_mcp_server.py` | 1521 / 1711 | 80 physical lines |
| `tests/test_searcher_positional_compatibility.py` | 0 / 7 | 4 physical lines |

The decision-node proxies remain 12 for the backend and 35 for the searcher;
the capacity helper has 12. These are declared AST proxies, not cyclomatic
complexity. Existing small memo/receipt helpers avoid duplicate implementations.
Keep this exception scoped to #51 and re-review before further growth in these
modules; bulk refactoring during reconciliation would expand risk.

Raw test receipts and original WIP copies are preserved privately in the
workspace `.tmp/fleet-git-hygiene-20260908/` folder. Six historical failure
diagnostics were moved from `.pytest-diagnostics/` into the private
`mempalace-original/` recovery folder with all six SHA256 values verified. They
remain generated private evidence and must not be published as source.

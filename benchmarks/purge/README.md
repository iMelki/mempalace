# Chroma Delete Multi-Arm Benchmark

Empirical evaluation of ChromaDB 1.5.7 delete latency across four filtering strategies on synthetic collections.

## Background & Hypothesis

In `mempalace/write_receipts.py`, purge and delete operations clean up obsolete rows during mining. A question arose whether combining document regex matching (`where_document={"$regex": ...}`) with metadata matching (`where={...}`) incurs an unacceptable latency penalty compared to raw ID deletion or metadata-only deletion.

## Test Arms

All arms operate against the **exact same synthetic collection** (`DIM=8`, `hnsw:space=cosine`, batch adds of 2,000 rows):

- **Arm A**: `col.delete(ids=[rid])` — ID-only deletion.
- **Arm B**: `col.delete(ids=[rid], where=where_for(meta))` — ID + metadata filter (`$and` over 4 metadata fields).
- **Arm C**: `col.delete(ids=[rid], where=where_for(meta), where_document=regex_for(doc))` — ID + metadata + exact document regex match (`(?s)^...$`).
- **Arm D**: `col.delete(ids=[rid], where_document=regex_for(doc))` — ID + document regex match only (decomposes Arm C).

## Apparatus Controls

Before timing measurements begin, six positive and negative controls verify filter correctness:
1. `ctl1`: `where` content-hash filter alone returns only the target row.
2. `ctl2`: `ids` + full `where` conjunction returns only the target row.
3. `ctl3`: `ids` + exact document regex returns only the target row.
4. `ctl4`: `ids` + `where` + regex returns only the target row.
5. `ctl5` (negative): `ids` + mismatched hash returns empty list `[]`.
6. `ctl6` (negative): `ids` + mismatched regex returns empty list `[]`.

All 6 controls passed (`controls_all_pass: true`). Zero rows survived deletion in both test runs (`delete_failures: []`, `failures: []`).

## Empirical Results (10,000-Row Collection, 10 Reps)

### Block Design Re-Measure (`results_block.json`)

Contiguous blocks per arm to prevent cross-arm WAL-compaction contamination:

| Arm | Filter Strategy | Min (ms) | P10 (ms) | Median (ms) | Mean (ms) | Max (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **A** | `ids` only | 26.80 | 29.28 | **30.92** | 32.45 | 47.81 |
| **B** | `ids` + metadata `where` | 31.19 | 33.49 | **35.79** | 35.40 | 39.45 |
| **C** | `ids` + metadata + doc regex | 69.32 | 74.85 | **80.75** | 81.60 | 100.13 |
| **D** | `ids` + doc regex only | 72.51 | 73.52 | **90.76** | 87.27 | 113.11 |

### Round-Robin Run (`results.json`)

| Arm | Filter Strategy | Min (ms) | Median (ms) | Mean (ms) | Max (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **A** | `ids` only | 55.07 | 172.33 | 242.01 | 622.04 |
| **B** | `ids` + metadata `where` | 79.76 | 176.34 | 212.40 | 520.41 |
| **C** | `ids` + metadata + doc regex | 151.36 | 362.09 | 353.45 | 706.03 |
| **D** | `ids` + doc regex only | 162.42 | 302.67 | 536.94 | 1616.83 |

## Findings & Recommendations

1. **Document Regex Penalty**: Deleting with `where_document` regex (Arms C & D) is **2.3× to 2.5× slower** than ID-only (Arm A) or ID + metadata filtering (Arm B).
2. **Metadata Filtering is Cheap**: Arm B adds only ~4.8 ms (15.7%) over raw ID deletion in the block design, while Arm C adds ~45 ms (126%).
3. **Architectural Recommendation**: For bulk purge and write receipts, use **Arm B** (`ids` + indexed metadata conjunction). Avoid `where_document` regex matching in delete paths unless content ambiguity cannot be resolved by metadata hash invariants.
4. **Prefer HOLD-fix (memsys#677)**: Managed remine purge in `write_receipts._delete_filters_for_validated_row` now always returns `where_document=None` on the hot path (ids + metadata `where` only), including legacy/missing or stale content-hash rows. No new rare/opt-in regex API was added.

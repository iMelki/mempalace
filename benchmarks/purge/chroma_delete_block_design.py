"""Block-design re-measure on the ALREADY-BUILT collections.

The round-robin run lets Chroma's WAL-compaction cost land on whichever arm
happens to trip the threshold, which contaminates arm A. Here each arm gets a
contiguous block of deletes, so compaction cost is charged to the arm that
caused it. Rows are read back from the collection (no RNG replay), and every
delete is verified to have removed exactly one row.
"""

import json
import os
import re
import sys
import time

import chromadb

assert chromadb.__version__ == "1.5.7", chromadb.__version__

ROOT = os.path.dirname(os.path.abspath(__file__))
SIZES = [int(x) for x in sys.argv[1].split(",")]
REPS = int(sys.argv[2]) if len(sys.argv) > 2 else 25
WARM = 3


def where_for(m):
    return {
        "$and": [
            {"write_source_identity": m["write_source_identity"]},
            {"source_file": m["source_file"]},
            {"write_receipt_id": m["write_receipt_id"]},
            {"write_output_content_hash": m["write_output_content_hash"]},
        ]
    }


def regex_for(d):
    return {"$regex": "(?s)^%s$" % re.escape(d)}


def run(size, reps, warm):
    path = os.path.join(ROOT, "db_%d" % size)
    client = chromadb.PersistentClient(path=path)
    col = client.get_collection("bench", embedding_function=None)
    n0 = col.count()

    # positive control on a row we will NOT delete
    ctl_row = col.get(ids=["row-00000010"], include=["documents", "metadatas"])
    assert ctl_row["ids"] == ["row-00000010"], ctl_row["ids"]
    cdoc, cmeta = ctl_row["documents"][0], ctl_row["metadatas"][0]
    ctl = {}
    r = col.get(where={"write_output_content_hash": cmeta["write_output_content_hash"]})
    ctl["where_contenthash_only_returns_only_row10"] = (r["ids"] == ["row-00000010"], r["ids"])
    r = col.get(ids=["row-00000010"], where=where_for(cmeta))
    ctl["ids_plus_full_where"] = (r["ids"] == ["row-00000010"], r["ids"])
    r = col.get(ids=["row-00000010"], where_document=regex_for(cdoc))
    ctl["ids_plus_regex"] = (r["ids"] == ["row-00000010"], r["ids"])
    r = col.get(ids=["row-00000010"], where=where_for(cmeta), where_document=regex_for(cdoc))
    ctl["ids_where_and_regex"] = (r["ids"] == ["row-00000010"], r["ids"])
    r = col.get(ids=["row-00000010"], where_document={"$regex": "(?s)^NOPE$"})
    ctl["neg_wrong_regex_returns_empty"] = (r["ids"] == [], r["ids"])

    # victims: evenly spaced, skip missing (already deleted by the round-robin run)
    need = (reps + warm) * 4
    cand, step, i = [], max(1, size // (need * 3)), 20
    while len(cand) < need * 2 and i < size:
        cand.append("row-%08d" % i)
        i += step
    got = col.get(ids=cand, include=["documents", "metadatas"])
    have = {rid: (got["documents"][k], got["metadatas"][k]) for k, rid in enumerate(got["ids"])}
    live = [c for c in cand if c in have][:need]
    assert len(live) == need, (len(live), need)

    res = {
        "size": size,
        "n_start": n0,
        "reps": reps,
        "controls": ctl,
        "controls_all_pass": all(v[0] for v in ctl.values()),
        "failures": [],
    }
    pos = 0
    for arm in ["A", "B", "C", "D"]:
        ts = []
        for j in range(warm + reps):
            rid = live[pos]
            pos += 1
            doc, meta = have[rid]
            kw = {"ids": [rid]}
            if arm in ("B", "C"):
                kw["where"] = where_for(meta)
            if arm in ("C", "D"):
                kw["where_document"] = regex_for(doc)
            t0 = time.perf_counter()
            col.delete(**kw)
            dt = (time.perf_counter() - t0) * 1000.0
            if j >= warm:
                ts.append(dt)
        # verify the whole block actually removed its rows (outside timing)
        blk = live[pos - (warm + reps) : pos]
        surv = col.get(ids=blk)["ids"]
        if surv:
            res["failures"].append((arm, surv))
        ts.sort()
        res[arm] = {
            "mean_ms": round(sum(ts) / len(ts), 2),
            "median_ms": round(ts[len(ts) // 2], 2),
            "p10_ms": round(ts[max(0, len(ts) // 10)], 2),
            "min_ms": round(ts[0], 2),
            "max_ms": round(ts[-1], 2),
        }
    res["n_end"] = col.count()
    res["expected_end"] = n0 - need
    res["count_ok"] = res["n_end"] == res["expected_end"]
    return res


if __name__ == "__main__":
    out = []
    for s in SIZES:
        r = run(s, REPS, WARM)
        out.append(r)
        print(json.dumps(r), flush=True)
        with open(os.path.join(ROOT, "results_block.json"), "w") as f:
            json.dump(out, f, indent=2)

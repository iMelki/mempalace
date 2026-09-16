"""Three-arm (plus a fourth decomposition arm) Chroma delete benchmark.

Arms, all on the SAME synthetic collection at the same size:
  A  delete(ids=[x])
  B  delete(ids=[x], where={...})                      metadata filter only, NO regex
  C  delete(ids=[x], where={...}, where_document=regex) both
  D  delete(ids=[x], where_document=regex)              regex only  (extra: decomposes C)

Row shape mirrors mempalace/write_receipts.py:2620 _delete_filters_for_validated_row.
"""

import hashlib
import json
import os
import random
import re
import shutil
import string
import sys
import time

import chromadb

assert chromadb.__version__ == "1.5.7", chromadb.__version__

ROOT = os.path.dirname(os.path.abspath(__file__))
DIM = 8
SIZES = [int(x) for x in sys.argv[1].split(",")] if len(sys.argv) > 1 else [10000, 40000, 160000]
REPS = int(sys.argv[2]) if len(sys.argv) > 2 else 25
WARMUP = 3

ALPHA = string.ascii_letters + string.digits + "     .,\n"


def make_doc(rng, n):
    return "".join(rng.choice(ALPHA) for _ in range(n))


def sha256_hex(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def build(size, seed=1234):
    rng = random.Random(seed)
    path = os.path.join(ROOT, "db_%d" % size)
    if os.path.isdir(path):
        shutil.rmtree(path)
    client = chromadb.PersistentClient(path=path)
    col = client.create_collection(
        name="bench", embedding_function=None, metadata={"hnsw:space": "cosine"}
    )
    rows = []
    B = 2000
    ids, docs, metas, embs = [], [], [], []
    t0 = time.perf_counter()
    for i in range(size):
        rid = "row-%08d" % i
        doc = make_doc(rng, rng.randint(300, 3000))
        h = sha256_hex(doc)
        meta = {
            "write_receipt_id": "%08x-%04x-4%03x-a%03x-%012x"
            % (
                rng.getrandbits(32),
                rng.getrandbits(16),
                rng.getrandbits(12),
                rng.getrandbits(12),
                rng.getrandbits(48),
            ),
            "write_source_identity": hashlib.sha256(("ident-%d" % (i % 97)).encode()).hexdigest(),
            "source_file": "S:/synthetic/source_%03d.md" % (i % 512),
            "write_output_content_hash": h,
        }
        ids.append(rid)
        docs.append(doc)
        metas.append(meta)
        embs.append([rng.random() for _ in range(DIM)])
        rows.append((rid, doc, meta))
        if len(ids) >= B:
            col.add(ids=ids, documents=docs, metadatas=metas, embeddings=embs)
            ids, docs, metas, embs = [], [], [], []
    if ids:
        col.add(ids=ids, documents=docs, metadatas=metas, embeddings=embs)
    build_s = time.perf_counter() - t0
    return client, col, rows, path, build_s


def where_for(meta):
    # exact conjunction order from write_receipts._delete_filters_for_validated_row
    return {
        "$and": [
            {"write_source_identity": meta["write_source_identity"]},
            {"source_file": meta["source_file"]},
            {"write_receipt_id": meta["write_receipt_id"]},
            {"write_output_content_hash": meta["write_output_content_hash"]},
        ]
    }


def regex_for(doc):
    return {"$regex": "(?s)^%s$" % re.escape(doc)}


def positive_control(col, rows):
    """Prove the apparatus: filters we expect to match DO return the row."""
    out = {}
    rid, doc, meta = rows[10]
    r = col.get(where={"write_output_content_hash": meta["write_output_content_hash"]})
    out["ctl1_where_contenthash_only"] = {
        "expect": [rid],
        "got": r["ids"],
        "pass": r["ids"] == [rid],
    }
    r = col.get(ids=[rid], where=where_for(meta))
    out["ctl2_ids_plus_full_where"] = {"expect": [rid], "got": r["ids"], "pass": r["ids"] == [rid]}
    r = col.get(ids=[rid], where_document=regex_for(doc))
    out["ctl3_ids_plus_regex"] = {"expect": [rid], "got": r["ids"], "pass": r["ids"] == [rid]}
    r = col.get(ids=[rid], where=where_for(meta), where_document=regex_for(doc))
    out["ctl4_ids_where_regex"] = {"expect": [rid], "got": r["ids"], "pass": r["ids"] == [rid]}
    # negative control: a where that must NOT match this id
    r = col.get(ids=[rid], where={"write_output_content_hash": sha256_hex("nope")})
    out["ctl5_negative_wrong_hash"] = {"expect": [], "got": r["ids"], "pass": r["ids"] == []}
    # negative control: regex that must NOT match
    r = col.get(ids=[rid], where_document={"$regex": "(?s)^ZZZ_NO_SUCH_DOC_ZZZ$"})
    out["ctl6_negative_wrong_regex"] = {"expect": [], "got": r["ids"], "pass": r["ids"] == []}
    return out


def run_size(size, reps, warmup):
    client, col, rows, path, build_s = build(size)
    n_start = col.count()
    ctl = positive_control(col, rows)
    ctl_ok = all(v["pass"] for v in ctl.values())

    rng = random.Random(99)
    # pick distinct victim indices, spread across the id space, never index 10
    pool = [i for i in range(size) if i != 10]
    rng.shuffle(pool)
    need = (reps + warmup) * 4
    victims = pool[:need]
    vi = iter(victims)

    timings = {"A": [], "B": [], "C": [], "D": []}
    failures = []

    def one(target_col, arm, idx):
        rid, doc, meta = rows[idx]
        kw = {"ids": [rid]}
        if arm in ("B", "C"):
            kw["where"] = where_for(meta)
        if arm in ("C", "D"):
            kw["where_document"] = regex_for(doc)
        t0 = time.perf_counter()
        target_col.delete(**kw)
        dt = (time.perf_counter() - t0) * 1000.0
        # correctness check OUTSIDE the timed region
        surv = target_col.get(ids=[rid])["ids"]
        if surv:
            failures.append((arm, rid, "survived"))
        return dt

    order = ["A", "B", "C", "D"]
    for _ in range(warmup):
        for arm in order:
            one(col, arm, next(vi))
    for _ in range(reps):
        for arm in order:
            timings[arm].append(one(col, arm, next(vi)))

    n_end = col.count()
    expected_end = n_start - need
    res = {
        "size": size,
        "build_s": round(build_s, 1),
        "reps": reps,
        "n_start": n_start,
        "n_end": n_end,
        "expected_end": expected_end,
        "count_ok": n_end == expected_end,
        "controls": ctl,
        "controls_all_pass": ctl_ok,
        "delete_failures": failures,
        "db_path": path,
    }
    for arm in order:
        v = sorted(timings[arm])
        res[arm] = {
            "mean_ms": round(sum(v) / len(v), 3),
            "median_ms": round(v[len(v) // 2], 3),
            "min_ms": round(v[0], 3),
            "max_ms": round(v[-1], 3),
        }
    return res


if __name__ == "__main__":
    allres = []
    for s in SIZES:
        r = run_size(s, REPS, WARMUP)
        allres.append(r)
        print(json.dumps(r, indent=2), flush=True)
        with open(os.path.join(ROOT, "results.json"), "w") as f:
            json.dump(allres, f, indent=2)

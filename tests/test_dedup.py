"""Tests for mempalace.dedup — near-duplicate drawer detection and removal."""

import re
import sys
from unittest.mock import MagicMock, patch


from mempalace import dedup


# ── get_source_groups ─────────────────────────────────────────────────


def test_get_source_groups_basic():
    col = MagicMock()
    col.count.return_value = 5
    col.get.side_effect = [
        {
            "ids": ["d1", "d2", "d3", "d4", "d5"],
            "metadatas": [
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
            ],
        },
        {"ids": []},
    ]
    groups = dedup.get_source_groups(col, min_count=5)
    assert "a.txt" in groups
    assert len(groups["a.txt"]) == 5


def test_get_source_groups_below_min():
    col = MagicMock()
    col.count.return_value = 2
    col.get.side_effect = [
        {
            "ids": ["d1", "d2"],
            "metadatas": [
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
            ],
        },
        {"ids": []},
    ]
    groups = dedup.get_source_groups(col, min_count=5)
    assert len(groups) == 0


def test_get_source_groups_source_filter():
    col = MagicMock()
    col.count.return_value = 6
    col.get.side_effect = [
        {
            "ids": ["d1", "d2", "d3", "d4", "d5", "d6"],
            "metadatas": [
                {"source_file": "project_a.txt"},
                {"source_file": "project_a.txt"},
                {"source_file": "project_a.txt"},
                {"source_file": "project_a.txt"},
                {"source_file": "project_a.txt"},
                {"source_file": "other.txt"},
            ],
        },
        {"ids": []},
    ]
    groups = dedup.get_source_groups(col, min_count=5, source_pattern="project_a")
    assert "project_a.txt" in groups
    assert "other.txt" not in groups


def test_get_source_groups_wing_filter():
    col = MagicMock()
    col.count.return_value = 5
    col.get.side_effect = [
        {
            "ids": ["d1", "d2", "d3", "d4", "d5"],
            "metadatas": [
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
            ],
        },
        {"ids": []},
    ]
    dedup.get_source_groups(col, min_count=5, wing="my_wing")
    # Verify where filter was passed
    first_call = col.get.call_args_list[0]
    assert first_call.kwargs.get("where") == {"wing": "my_wing"}


def test_get_source_groups_missing_source_file():
    col = MagicMock()
    col.count.return_value = 5
    col.get.side_effect = [
        {
            "ids": ["d1", "d2", "d3", "d4", "d5"],
            "metadatas": [{}, {}, {}, {}, {}],
        },
        {"ids": []},
    ]
    groups = dedup.get_source_groups(col, min_count=5)
    assert "unknown" in groups


# ── dedup_source_group ────────────────────────────────────────────────


def test_dedup_source_group_all_unique():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": ["long document one content here", "different document two here"],
        "metadatas": [{"wing": "a"}, {"wing": "a"}],
    }
    col.query.return_value = {
        "ids": [["d1"]],
        "distances": [[0.8]],  # far apart = unique
    }
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert len(kept) == 2
    assert len(deleted) == 0


def test_dedup_source_group_with_duplicate():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": [
            "long document content that is fairly long",
            "long document content that is fairly long",
        ],
        "metadatas": [{"wing": "a"}, {"wing": "a"}],
    }
    col.query.return_value = {
        "ids": [["d1"]],
        "distances": [[0.05]],  # very close = duplicate
    }
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert len(kept) == 1
    assert len(deleted) == 1


def test_dedup_source_group_short_docs_deleted():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": ["long enough document to keep in the palace", "tiny"],
        "metadatas": [{"wing": "a"}, {"wing": "a"}],
    }
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert "d2" in deleted  # too short


def test_dedup_source_group_empty_doc_deleted():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": ["real document content here that is long enough", None],
        "metadatas": [{"wing": "a"}, {"wing": "a"}],
    }
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert "d2" in deleted


def test_dedup_source_group_live_deletes():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": ["long document content here enough", "long document content here enough"],
        "metadatas": [{"wing": "a"}, {"wing": "a"}],
    }
    col.query.return_value = {
        "ids": [["d1"]],
        "distances": [[0.05]],
    }
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=False)
    col.delete.assert_called_once()


def test_dedup_source_group_query_failure_keeps():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": [
            "long document one content here enough",
            "long document two content here enough",
        ],
        "metadatas": [{"wing": "a"}, {"wing": "a"}],
    }
    col.query.side_effect = Exception("query failed")
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert len(kept) == 2  # both kept on error


# ── show_stats ────────────────────────────────────────────────────────


def _install_mock_backend(mock_backend_cls, collection):
    mock_backend = MagicMock()
    mock_backend.get_collection.return_value = collection
    mock_backend_cls.return_value = mock_backend
    return mock_backend


@patch("mempalace.dedup.ChromaBackend")
def test_show_stats(mock_backend_cls, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 5
    mock_col.get.side_effect = [
        {
            "ids": ["d1", "d2", "d3", "d4", "d5"],
            "metadatas": [
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
            ],
        },
        {"ids": []},
    ]
    _install_mock_backend(mock_backend_cls, mock_col)

    dedup.show_stats(palace_path=str(tmp_path))  # should not raise


# ── show_stats scoping + honest duplicate counts (iMelki/mempalace#33) ──


@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_show_stats_threads_wing_and_source_to_get_source_groups(
    mock_backend_cls, mock_get_groups, tmp_path
):
    """Regression: show_stats used to drop wing/source, silently scanning all."""
    mock_col = MagicMock()
    _install_mock_backend(mock_backend_cls, mock_col)
    mock_get_groups.return_value = {"a.txt": ["d1", "d2"]}

    dedup.show_stats(palace_path=str(tmp_path), wing="coding", source_pattern="repeater")

    assert mock_get_groups.call_count == 1
    kwargs = mock_get_groups.call_args.kwargs
    assert kwargs["wing"] == "coding"
    assert kwargs["source_pattern"] == "repeater"


@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_show_stats_prints_active_scope(mock_backend_cls, mock_get_groups, tmp_path, capsys):
    """A silently-unscoped run must be visible in the output."""
    _install_mock_backend(mock_backend_cls, MagicMock())
    mock_get_groups.return_value = {"a.txt": ["d1", "d2"]}

    dedup.show_stats(palace_path=str(tmp_path), wing="coding")
    out = capsys.readouterr().out

    assert "wing=coding" in out


@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_show_stats_emits_no_fabricated_estimate(
    mock_backend_cls, mock_get_groups, tmp_path, capsys
):
    """The `len(ids) * 0.4` pseudo-metric must not come back.

    A single source legitimately chunked into many drawers is not duplication,
    so a large group must NOT by itself produce a duplicate claim.
    """
    _install_mock_backend(mock_backend_cls, MagicMock())
    mock_get_groups.return_value = {"big.txt": [f"d{i}" for i in range(100)]}

    dedup.show_stats(palace_path=str(tmp_path))
    out = capsys.readouterr().out

    assert "Estimated duplicates" not in out
    # no 0.4-derived figure (100 * 0.4 = 40). Matched on a word boundary so an
    # unrelated number in the metrics line (e.g. a 0.405s duration) cannot make
    # this flake, while a literal "40" duplicate claim still fails it.
    assert not re.search(r"\b40\b", out)
    assert "not computed" in out


@patch("mempalace.dedup.ChromaBackend")
def test_show_stats_exact_duplicates_counts_real_redundancy(mock_backend_cls, tmp_path, capsys):
    """3 identical + 1 unique => 1 duplicate set, 2 redundant drawers."""
    mock_col = MagicMock()
    mock_col.get.return_value = {"documents": ["same text", "same text", "same text", "other"]}
    _install_mock_backend(mock_backend_cls, mock_col)

    with patch.object(dedup, "get_source_groups", return_value={"a.txt": ["d1", "d2", "d3", "d4"]}):
        dedup.show_stats(palace_path=str(tmp_path), exact_duplicates=True)

    out = capsys.readouterr().out
    assert "Exact-duplicate sets (byte-identical documents): 1" in out
    assert "Redundant drawers in those sets:                 2" in out


def test_count_exact_duplicates_ignores_unique_and_empty_docs():
    col = MagicMock()
    col.get.return_value = {"documents": ["a", "b", "c", None, ""]}
    dup_groups, redundant = dedup.count_exact_duplicates(col, {"s": ["1", "2", "3", "4", "5"]})
    assert (dup_groups, redundant) == (0, 0)


def test_count_exact_duplicates_two_separate_sets():
    col = MagicMock()
    col.get.return_value = {"documents": ["x", "x", "y", "y", "y", "z"]}
    dup_groups, redundant = dedup.count_exact_duplicates(col, {"s": ["1", "2", "3", "4", "5", "6"]})
    # {x:2, y:3} -> 2 sets; redundant = (2-1) + (3-1) = 3
    assert (dup_groups, redundant) == (2, 3)


def test_count_exact_duplicates_does_not_merge_across_source_groups():
    """Identical text in two different sources is not intra-source redundancy."""
    col = MagicMock()
    col.get.return_value = {"documents": ["same"]}
    dup_groups, redundant = dedup.count_exact_duplicates(col, {"a": ["1"], "b": ["2"]})
    assert (dup_groups, redundant) == (0, 0)


# ── count_cross_source_duplicates (iMelki/mempalace#19) ───────────────


def _docs_by_id(mapping):
    """col.get(ids=...) stub that returns the documents for the requested ids."""

    def _get(ids=None, include=None, **kwargs):
        return {"ids": list(ids), "documents": [mapping[i] for i in ids]}

    return _get


def test_count_cross_source_duplicates_merges_across_source_groups():
    """The whole point: identical text under different source_file paths is one set."""
    col = MagicMock()
    col.get.side_effect = _docs_by_id({"1": "same text", "2": "same text"})

    res = dedup.count_cross_source_duplicates(col, {"a.txt": ["1"], "b.txt": ["2"]})

    assert res["sets"] == 1
    assert res["redundant"] == 1
    assert res["cross_path_sets"] == 1
    assert res["cross_path_redundant"] == 1
    assert res["single_path_sets"] == 0


def test_count_cross_source_duplicates_reports_contributing_source_paths():
    """Five on-disk copies of one project: the operator must see WHICH paths."""
    paths = [
        r"S:\source\EMTS\Repeater_System\repeater-system\assets\js\bui.js",
        r"S:\source\EMTS\Repeater_System\repeater-system-mobile-fixes\assets\js\bui.js",
        r"S:\source\EMTS\Repeater_System\repeater-system-all-ui-alpha-ver\assets\js\bui.js",
        r"S:\source\EMTS\Repeater_System\repeater-system_backup_250522\assets\js\bui.js",
        r"S:\source\EMTS\Repeater_System\files\bui.js",
    ]
    groups = {p: [f"d{i}"] for i, p in enumerate(paths)}
    col = MagicMock()
    col.get.side_effect = _docs_by_id({f"d{i}": "identical asset body" for i in range(len(paths))})

    res = dedup.count_cross_source_duplicates(col, groups)

    assert (res["sets"], res["redundant"]) == (1, 4)
    top = res["top_sets"][0]
    assert top["drawers"] == 5
    assert top["distinct_sources"] == 5
    assert [p for p, _ in top["sources"]] == sorted(paths)
    assert all(count == 1 for _, count in top["sources"])
    assert top["snippet"] == "identical asset body"


def test_count_cross_source_duplicates_separates_single_path_repeats():
    """Repeats inside ONE source are counted but bucketed apart from cross-path ones."""
    col = MagicMock()
    col.get.side_effect = _docs_by_id({"1": "dup", "2": "dup", "3": "solo"})

    res = dedup.count_cross_source_duplicates(col, {"a.txt": ["1", "2", "3"]})

    assert (res["sets"], res["redundant"]) == (1, 1)
    assert (res["cross_path_sets"], res["cross_path_redundant"]) == (0, 0)
    assert (res["single_path_sets"], res["single_path_redundant"]) == (1, 1)


def test_cross_source_absorbs_intra_source_sets_that_also_span_paths():
    """The single-path bucket is NOT the intra-source number.

    Text repeated inside A that also appears in B is ONE merged set classified
    as cross-path, not an intra-source set plus a cross-source set. This is why
    the single-path figure can be lower than the intra-source figure over the
    same drawers (measured: 73 vs 83 on wing=coding).
    """
    docs = {"a1": "X", "a2": "X", "a3": "X", "b1": "X", "b2": "X"}
    groups = {"A": ["a1", "a2", "a3"], "B": ["b1", "b2"]}

    intra_col = MagicMock()
    intra_col.get.side_effect = _docs_by_id(docs)
    assert dedup.count_exact_duplicates(intra_col, groups) == (2, 3)

    cross_col = MagicMock()
    cross_col.get.side_effect = _docs_by_id(docs)
    res = dedup.count_cross_source_duplicates(cross_col, groups)

    assert (res["sets"], res["redundant"]) == (1, 4)
    assert (res["cross_path_sets"], res["cross_path_redundant"]) == (1, 4)
    assert (res["single_path_sets"], res["single_path_redundant"]) == (0, 0)
    # the merged set names both paths with their per-path drawer counts
    assert res["top_sets"][0]["sources"] == [("A", 3), ("B", 2)]


def test_count_cross_source_duplicates_ignores_unique_and_empty_docs():
    col = MagicMock()
    col.get.side_effect = _docs_by_id({"1": "a", "2": "b", "3": None, "4": ""})

    res = dedup.count_cross_source_duplicates(col, {"a.txt": ["1", "2"], "b.txt": ["3", "4"]})

    assert (res["sets"], res["redundant"]) == (0, 0)
    assert res["top_sets"] == []
    assert res["drawers_scanned"] == 4


def test_count_cross_source_duplicates_batches_and_never_reads_whole_corpus():
    """Host C: has ~2% free: batch, do not slurp and do not spill to disk."""
    ids = [f"d{i}" for i in range(1200)]
    col = MagicMock()
    col.get.side_effect = _docs_by_id({i: f"body-{i}" for i in ids})

    res = dedup.count_cross_source_duplicates(col, {"big.txt": ids})

    sizes = [len(call.kwargs["ids"]) for call in col.get.call_args_list]
    assert sizes == [500, 500, 200]
    assert max(sizes) <= dedup.HASH_BATCH_SIZE
    assert res["drawers_scanned"] == 1200


def test_count_cross_source_duplicates_honours_custom_batch_size():
    ids = [f"d{i}" for i in range(7)]
    col = MagicMock()
    col.get.side_effect = _docs_by_id({i: f"body-{i}" for i in ids})

    dedup.count_cross_source_duplicates(col, {"s": ids}, batch_size=3)

    assert [len(call.kwargs["ids"]) for call in col.get.call_args_list] == [3, 3, 1]


def test_count_cross_source_duplicates_emits_progress(capsys):
    col = MagicMock()
    col.get.side_effect = _docs_by_id({"1": "x", "2": "x"})

    dedup.count_cross_source_duplicates(col, {"a": ["1"], "b": ["2"]}, progress=True)
    captured = capsys.readouterr()

    # stderr, not stdout: stdout is the report channel (iMelki/mempalace#32)
    assert "cross-source duplicate scan:" in captured.err
    assert "100.0%" in captured.err
    assert "done, 2 processed" in captured.err
    assert "cross-source duplicate scan" not in captured.out


def test_count_cross_source_duplicates_max_sets_bounds_output_not_counts():
    docs = {}
    groups = {}
    for s in range(5):  # 5 distinct duplicate sets, 2 drawers each
        groups[f"src{s}"] = [f"d{s}a", f"d{s}b"]
        docs[f"d{s}a"] = f"text-{s}"
        docs[f"d{s}b"] = f"text-{s}"
    col = MagicMock()
    col.get.side_effect = _docs_by_id(docs)

    res = dedup.count_cross_source_duplicates(col, groups, max_sets=2)

    assert (res["sets"], res["redundant"]) == (5, 5)
    assert len(res["top_sets"]) == 2


def test_count_cross_source_duplicates_ranks_largest_set_first():
    docs = {"a1": "big", "a2": "big", "a3": "big", "b1": "small", "b2": "small"}
    col = MagicMock()
    col.get.side_effect = _docs_by_id(docs)

    res = dedup.count_cross_source_duplicates(
        col, {"a.txt": ["a1", "a2", "a3"], "b.txt": ["b1", "b2"]}
    )

    assert [s["drawers"] for s in res["top_sets"]] == [3, 2]


def test_count_cross_source_duplicates_does_not_pick_a_canonical_copy():
    """Redundant is N-1, but no winner/loser is named: that is operator policy."""
    col = MagicMock()
    col.get.side_effect = _docs_by_id({"1": "same", "2": "same", "3": "same"})

    res = dedup.count_cross_source_duplicates(col, {"a": ["1"], "b": ["2"], "c": ["3"]})

    assert res["redundant"] == 2  # N-1, not "delete these 2 ids"
    assert set(res) == {
        "drawers_scanned",
        "sets",
        "redundant",
        "cross_path_sets",
        "cross_path_redundant",
        "single_path_sets",
        "single_path_redundant",
        "same_filename_sets",
        "same_filename_redundant",
        "top_sets",
    }
    for key in ("keep", "canonical", "delete", "to_delete", "winner", "ids"):
        assert key not in res
        assert key not in res["top_sets"][0]
    # contributing paths are reported symmetrically — path + count only
    assert all(
        set(k)
        == {
            "drawers",
            "redundant",
            "distinct_sources",
            "distinct_filenames",
            "sources",
            "snippet",
        }
        for k in res["top_sets"]
    )


def test_cross_source_splits_copied_file_from_shared_boilerplate():
    """One filename in many trees is a copied directory; many filenames is not.

    Both are byte-identical content stored N times, but only the first is the
    "five copies of one project" artifact #19 is about, and their deletion
    policies differ sharply. The split is reported, not judged.
    """
    docs = {
        # same file, three project copies
        "c1": "COPIED",
        "c2": "COPIED",
        "c3": "COPIED",
        # different files sharing one chunk (CSS skins)
        "s1": "SHARED",
        "s2": "SHARED",
    }
    groups = {
        r"S:\proj\Css\bootstrap.css": ["c1"],
        r"S:\proj-backup\Css\bootstrap.css": ["c2"],
        r"S:\proj-mobile-fixes\Css\bootstrap.css": ["c3"],
        r"S:\proj\skins\aero.css": ["s1"],
        r"S:\proj\skins\black.css": ["s2"],
    }
    col = MagicMock()
    col.get.side_effect = _docs_by_id(docs)

    res = dedup.count_cross_source_duplicates(col, groups)

    assert (res["cross_path_sets"], res["cross_path_redundant"]) == (2, 3)
    # only the bootstrap.css set has a single filename across its paths
    assert (res["same_filename_sets"], res["same_filename_redundant"]) == (1, 2)
    by_size = {s["drawers"]: s for s in res["top_sets"]}
    assert by_size[3]["distinct_filenames"] == 1
    assert by_size[2]["distinct_filenames"] == 2


def test_basename_splits_both_separators():
    """os.path.basename() would return a Windows path whole on Linux CI."""
    assert dedup._basename(r"S:\a\b\c.css") == "c.css"
    assert dedup._basename("/srv/a/b/c.css") == "c.css"
    assert dedup._basename("c.css") == "c.css"


def test_count_cross_source_duplicates_never_deletes():
    col = MagicMock()
    col.get.side_effect = _docs_by_id({"1": "same", "2": "same"})

    dedup.count_cross_source_duplicates(col, {"a": ["1"], "b": ["2"]})

    col.delete.assert_not_called()
    col.update.assert_not_called()
    col.upsert.assert_not_called()


def test_cross_source_mode_is_not_reachable_from_any_mutation_path():
    """Read-only by construction: the deletion path must not know this mode."""
    import inspect

    assert "cross_source" not in inspect.getsource(dedup.dedup_palace)
    assert "cross_source" not in inspect.getsource(dedup.dedup_source_group)
    for fn in (dedup.count_cross_source_duplicates, dedup.print_cross_source_report):
        src = inspect.getsource(fn)
        assert ".delete(" not in src
        assert ".upsert(" not in src


def _cross_result(**overrides):
    """A complete cross-source result, so printer fixtures cannot drift from the
    real shape when a field is added (the key set itself is asserted by
    test_count_cross_source_duplicates_does_not_pick_a_canonical_copy)."""
    base = {
        "drawers_scanned": 0,
        "sets": 0,
        "redundant": 0,
        "cross_path_sets": 0,
        "cross_path_redundant": 0,
        "single_path_sets": 0,
        "single_path_redundant": 0,
        "same_filename_sets": 0,
        "same_filename_redundant": 0,
        "top_sets": [],
    }
    base.update(overrides)
    return base


def _top_set(**overrides):
    base = {
        "drawers": 2,
        "redundant": 1,
        "distinct_sources": 2,
        "distinct_filenames": 1,
        "sources": [("a", 1), ("b", 1)],
        "snippet": "x",
    }
    base.update(overrides)
    return base


def test_print_cross_source_report_shows_counts_paths_and_no_winner(capsys):
    result = _cross_result(
        drawers_scanned=10,
        sets=2,
        redundant=6,
        cross_path_sets=1,
        cross_path_redundant=4,
        single_path_sets=1,
        single_path_redundant=2,
        same_filename_sets=1,
        same_filename_redundant=4,
        top_sets=[
            _top_set(
                drawers=5,
                redundant=4,
                sources=[(r"S:\a\bui.js", 3), (r"S:\b\bui.js", 2)],
                snippet="asset body",
            )
        ],
    )

    dedup.print_cross_source_report(result)
    out = capsys.readouterr().out

    assert "Cross-source exact-duplicate sets (grouped across source_file): 2" in out
    assert "6" in out
    assert "of those, sets whose paths all share one filename: 1  (4 redundant)" in out
    assert r"S:\a\bui.js" in out and r"S:\b\bui.js" in out
    assert "No canonical copy is chosen" in out


def test_print_cross_source_report_truncates_long_path_lists(capsys):
    sources = [(f"S:/p{i}/f.js", 1) for i in range(12)]
    result = _cross_result(
        drawers_scanned=12,
        sets=1,
        redundant=11,
        cross_path_sets=1,
        cross_path_redundant=11,
        top_sets=[
            _top_set(drawers=12, redundant=11, distinct_sources=12, sources=sources),
        ],
    )

    dedup.print_cross_source_report(result, max_paths=8)
    out = capsys.readouterr().out

    assert "S:/p7/f.js" in out
    assert "S:/p8/f.js" not in out
    assert "+4 more source path(s)" in out


def test_doc_snippet_collapses_whitespace_and_truncates():
    assert dedup._doc_snippet("  a\n\tb   c ") == "a b c"
    assert dedup._doc_snippet("x" * 200, limit=10) == "x" * 10 + "..."
    assert dedup._doc_snippet(None) == ""


# ── console-encoding safety (live crash on cp1252, 2026-08-06) ─────────


def _cp1252_stdout(monkeypatch):
    """A strict cp1252 stdout, i.e. a default Windows console."""
    import io

    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="cp1252", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", stream)
    return buf, stream


def test_printable_replaces_characters_the_console_cannot_encode(monkeypatch):
    _cp1252_stdout(monkeypatch)
    assert dedup._printable("Tahoma, STHeiti 字体") == "Tahoma, STHeiti ??"
    assert dedup._printable("plain ascii") == "plain ascii"


def test_printable_falls_back_when_stdout_encoding_is_unusable(monkeypatch):
    class _NoEncoding:
        encoding = "not-a-real-codec"

    monkeypatch.setattr(sys, "stdout", _NoEncoding())
    assert dedup._printable("字体") == "??"


def test_print_cross_source_report_survives_cp1252_console(monkeypatch):
    """Regression: a CJK font name in a CSS chunk killed the report mid-print.

    Built from the real scan output, not a hand-written dict, so this exercises
    the same path the live run took.
    """
    css = "font-family: Tahoma, Arial, Helvetica, STHeiti 字体;"
    col = MagicMock()
    col.get.side_effect = _docs_by_id({"d1": css, "d2": css})
    result = dedup.count_cross_source_duplicates(
        col, {"S:/项目/a.css": ["d1"], "S:/copy/a.css": ["d2"]}
    )

    buf, stream = _cp1252_stdout(monkeypatch)
    dedup.print_cross_source_report(result)  # must not raise UnicodeEncodeError
    stream.flush()
    out = buf.getvalue().decode("cp1252")

    assert "Cross-source exact-duplicate sets" in out
    assert "S:/copy/a.css" in out
    assert "?" in out  # unencodable characters were replaced, not fatal
    assert "No canonical copy is chosen" in out


@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_show_stats_source_listing_survives_cp1252_console(
    mock_backend_cls, mock_get_groups, monkeypatch, tmp_path
):
    """The top-15 listing prints mined paths, which are arbitrary content too."""
    buf, stream = _cp1252_stdout(monkeypatch)
    _install_mock_backend(mock_backend_cls, MagicMock())
    mock_get_groups.return_value = {"S:/项目/f.txt": ["d1", "d2"]}

    dedup.show_stats(palace_path=str(tmp_path))  # must not raise
    stream.flush()
    out = buf.getvalue().decode("cp1252")

    assert "Top 15 by drawer count" in out
    assert "S:/??/f.txt" in out


# ── show_stats cross-source wiring ────────────────────────────────────


@patch("mempalace.dedup.ChromaBackend")
def test_show_stats_cross_source_reports_sets_redundant_and_paths(
    mock_backend_cls, tmp_path, capsys
):
    """#19 requires BOTH figures plus the contributing paths."""
    mock_col = MagicMock()
    mock_col.get.side_effect = _docs_by_id({"d1": "same body", "d2": "same body"})
    _install_mock_backend(mock_backend_cls, mock_col)

    groups = {r"S:\copy-one\f.js": ["d1"], r"S:\copy-two\f.js": ["d2"]}
    with patch.object(dedup, "get_source_groups", return_value=groups):
        dedup.show_stats(palace_path=str(tmp_path), cross_source_duplicates=True)

    out = capsys.readouterr().out
    assert "Cross-source exact-duplicate sets (grouped across source_file): 1" in out
    assert "Redundant drawers in those sets (N-1 per set):" in out
    assert r"S:\copy-one\f.js" in out
    assert r"S:\copy-two\f.js" in out
    mock_col.delete.assert_not_called()


@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_show_stats_cross_source_scans_sources_below_min_count(
    mock_backend_cls, mock_get_groups, tmp_path, capsys
):
    """Five copies of one file are 1 drawer each — min_count would hide them."""
    mock_col = MagicMock()
    mock_col.get.side_effect = _docs_by_id({"d1": "same", "d2": "same"})
    _install_mock_backend(mock_backend_cls, mock_col)
    mock_get_groups.return_value = {"a.txt": ["d1"], "b.txt": ["d2"]}

    dedup.show_stats(palace_path=str(tmp_path), cross_source_duplicates=True, wing="coding")

    # one metadata pass, unfiltered, still honouring the wing scope
    assert mock_get_groups.call_count == 1
    kwargs = mock_get_groups.call_args.kwargs
    assert kwargs["min_count"] == 1
    assert kwargs["wing"] == "coding"

    out = capsys.readouterr().out
    assert "Sources with 5+ drawers: 0" in out  # the min_count view is still filtered
    assert "Drawers in scope incl. sources under min_count: 2 across 2 sources" in out
    assert "Cross-source exact-duplicate sets (grouped across source_file): 1" in out


@patch("mempalace.dedup.ChromaBackend")
def test_show_stats_reports_both_modes_separately(mock_backend_cls, tmp_path, capsys):
    """Intra-source and cross-source numbers must reconcile, not replace each other."""
    docs = {"d1": "same", "d2": "same", "d3": "same"}
    mock_col = MagicMock()
    mock_col.get.side_effect = _docs_by_id(docs)
    _install_mock_backend(mock_backend_cls, mock_col)

    # a.txt holds 2 identical drawers (intra-source visible), b.txt holds a third
    groups = {"a.txt": ["d1", "d2"], "b.txt": ["d3"]}
    with patch.object(dedup, "get_source_groups", return_value=groups):
        dedup.show_stats(
            palace_path=str(tmp_path),
            min_count=2,
            exact_duplicates=True,
            cross_source_duplicates=True,
        )

    out = capsys.readouterr().out
    # intra-source sees only a.txt's pair
    assert "Exact-duplicate sets (byte-identical documents): 1" in out
    assert "Redundant drawers in those sets:                 1" in out
    # cross-source merges all three
    assert "Cross-source exact-duplicate sets (grouped across source_file): 1" in out
    assert "sets spanning 2+ distinct source paths: 1  (2 redundant)" in out
    assert "not computed" not in out


@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_show_stats_exact_duplicates_points_at_the_cross_source_blind_spot(
    mock_backend_cls, mock_get_groups, tmp_path, capsys
):
    mock_col = MagicMock()
    mock_col.get.return_value = {"documents": ["a", "b"]}
    _install_mock_backend(mock_backend_cls, mock_col)
    mock_get_groups.return_value = {"a.txt": ["d1", "d2"]}

    dedup.show_stats(palace_path=str(tmp_path), exact_duplicates=True)
    out = capsys.readouterr().out

    assert "--cross-source-duplicates" in out
    assert mock_get_groups.call_args.kwargs["min_count"] == dedup.MIN_DRAWERS_TO_CHECK


@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_show_stats_default_pass_still_computes_nothing(
    mock_backend_cls, mock_get_groups, tmp_path, capsys
):
    """The metadata-only default must stay cheap and must not run a hash pass."""
    mock_col = MagicMock()
    _install_mock_backend(mock_backend_cls, mock_col)
    mock_get_groups.return_value = {"a.txt": ["d1", "d2"]}

    dedup.show_stats(palace_path=str(tmp_path))
    out = capsys.readouterr().out

    assert "not computed" in out
    assert "Cross-source" not in out
    mock_col.get.assert_not_called()


def test_cli_cross_source_requires_stats_and_accepts_alias():
    """--cross-source(-duplicates) is a --stats-only read mode, alias included."""
    import subprocess
    import sys

    for flag in ("--cross-source-duplicates", "--cross-source"):
        res = subprocess.run(
            [sys.executable, "-m", "mempalace.dedup", flag],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert res.returncode == 2, res.stderr
        assert "--cross-source-duplicates only applies to --stats" in res.stderr
        assert "unrecognized" not in res.stderr  # proves the alias parsed


# ── get_cpu_usage never shells out by bare name (projects-ops#115) ─────


def test_get_cpu_usage_wmic_fallback_uses_absolute_path(monkeypatch):
    """Bare `wmic` cannot resolve on a host whose PATH lacks System32."""
    monkeypatch.setitem(__import__("sys").modules, "psutil", None)  # force fallback
    recorded = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        raise RuntimeError("stop after capturing argv")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("os.path.isfile", lambda p: True)

    dedup.get_cpu_usage()  # swallows the error by design

    assert recorded["cmd"][0].lower().endswith("wmic.exe")
    assert "system32" in recorded["cmd"][0].lower()


# ── dedup_palace ──────────────────────────────────────────────────────


@patch("mempalace.dedup.dedup_source_group")
@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_dedup_palace_dry_run(mock_backend_cls, mock_groups, mock_dedup_group, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 10
    _install_mock_backend(mock_backend_cls, mock_col)

    mock_groups.return_value = {"a.txt": ["d1", "d2", "d3", "d4", "d5"]}
    mock_dedup_group.return_value = (["d1", "d2", "d3"], ["d4", "d5"])

    dedup.dedup_palace(palace_path=str(tmp_path), dry_run=True)
    mock_dedup_group.assert_called_once()


@patch("mempalace.dedup.dedup_source_group")
@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_dedup_palace_with_wing(mock_backend_cls, mock_groups, mock_dedup_group, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 10
    _install_mock_backend(mock_backend_cls, mock_col)

    mock_groups.return_value = {}
    dedup.dedup_palace(palace_path=str(tmp_path), wing="test_wing", dry_run=True)
    mock_groups.assert_called_once_with(mock_col, 5, None, wing="test_wing", progress=False)


@patch("mempalace.dedup.dedup_source_group")
@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_dedup_palace_no_groups(mock_backend_cls, mock_groups, mock_dedup_group, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 3
    _install_mock_backend(mock_backend_cls, mock_col)

    mock_groups.return_value = {}
    dedup.dedup_palace(palace_path=str(tmp_path), dry_run=True)
    mock_dedup_group.assert_not_called()


def test_cli_bare_invocation_defaults_to_dry_run():
    """Safety regression (iMelki/mempalace#19): bare `python -m mempalace.dedup`
    must be a dry-run preview; live deletion requires an explicit --apply."""
    import subprocess
    import sys

    # --apply and --dry-run together must be rejected (argparse error, exit 2)
    res = subprocess.run(
        [sys.executable, "-m", "mempalace.dedup", "--apply", "--dry-run"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 2
    assert "mutually exclusive" in res.stderr

    # bare invocation against a nonexistent palace should never reach a live
    # delete path; we only assert the parser wiring here (dry_run=not apply)
    src = open("mempalace/dedup.py", encoding="utf-8").read()
    assert "dry_run=not args.apply" in src


def test_live_dedup_prewarns_palace_after_deletions(monkeypatch, capsys):
    """iMelki/mempalace#19: after live deletions, dedup must pre-warm the
    palace so the one-time post-mutation open cost is paid at mutation time."""
    calls = []

    import mempalace.searcher as searcher

    monkeypatch.setattr(
        searcher,
        "search_memories",
        lambda *a, **k: calls.append((a, k)) or {"results": []},
    )
    monkeypatch.setattr(dedup, "get_source_groups", lambda *a, **k: {"src.md": ["d1", "d2"]})
    monkeypatch.setattr(dedup, "dedup_source_group", lambda *a, **k: (["d1"], ["d2"]))

    class _FakeCol:
        def count(self):
            return 2

    class _FakeBackend:
        def get_collection(self, *a, **k):
            return _FakeCol()

    monkeypatch.setattr(dedup, "ChromaBackend", lambda: _FakeBackend())

    dedup.dedup_palace(palace_path="X:/fake-palace", dry_run=False)
    out = capsys.readouterr().out
    assert "Pre-warming palace" in out
    assert calls, "post-deletion warm did not invoke search_memories"

    # dry runs must NOT warm
    calls.clear()
    dedup.dedup_palace(palace_path="X:/fake-palace", dry_run=True)
    assert not calls


# ── progress on every long pass (iMelki/mempalace#32) ─────────────────
#
# #33 added --progress for the exact-duplicate content scan only. The metadata
# page loop and the embedding-distance dry-run — the pass that precedes any
# --apply, and the genuinely long one — stayed silent. These tests pin all of
# them, the stderr-only stream, and the off-by-default behaviour.


def _metadata_col(count, pages):
    """A collection whose paged get() returns `pages` of (id, source) rows."""
    col = MagicMock()
    col.count.return_value = count
    col.get.side_effect = [
        {
            "ids": [did for did, _ in page],
            "metadatas": [{"source_file": src} for _, src in page],
        }
        for page in pages
    ]
    return col


def test_get_source_groups_emits_page_loop_progress_to_stderr(capsys):
    """Gap 1: on a ~1M-drawer palace this loop is the slow part before any
    duplicate work starts, so it needs its own heartbeat."""
    col = _metadata_col(3, [[("d1", "a"), ("d2", "a"), ("d3", "a")]])

    dedup.get_source_groups(col, min_count=1, progress=True)
    captured = capsys.readouterr()

    assert "metadata scan:" in captured.err
    assert "100.0%" in captured.err
    assert "done, 3 processed" in captured.err
    assert "1 sources with 1+ drawers" in captured.err
    assert captured.out == ""  # stdout stays a clean report channel


def test_get_source_groups_is_silent_by_default(capsys):
    """Non-interactive callers and tests must be unaffected."""
    col = _metadata_col(3, [[("d1", "a"), ("d2", "a"), ("d3", "a")]])

    dedup.get_source_groups(col, min_count=1)
    captured = capsys.readouterr()

    assert captured.err == ""
    assert captured.out == ""


def test_get_source_groups_progress_clamps_a_wing_scoped_denominator(capsys):
    """The denominator is col.count() (whole collection) while a wing `where`
    clause returns far fewer rows, so the loop ends well below 100%. The closing
    line must still state the real number rather than a misleading percentage."""
    col = MagicMock()
    col.count.return_value = 1000
    col.get.side_effect = [
        {"ids": ["d1", "d2"], "metadatas": [{"source_file": "a"}, {"source_file": "a"}]},
        {"ids": []},
    ]

    dedup.get_source_groups(col, min_count=1, wing="coding", progress=True)
    err = capsys.readouterr().err

    assert "  0.2%" in err  # 2 of 1000, not clamped up to 100
    assert "done, 2 processed" in err


def test_dedup_source_group_reports_progress_once_per_drawer():
    """Gap 2, inner loop: the unit of work is one col.query per drawer, and the
    empty/short-document early-outs must report too or a group of short drawers
    would look stalled."""
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2", "d3"],
        "documents": ["a document body long enough to survive", "short", ""],
        "metadatas": [{}, {}, {}],
    }
    col.query.return_value = {"ids": [["d1"]], "distances": [[0.9]]}
    ticks = []

    kept, deleted = dedup.dedup_source_group(
        col, ["d1", "d2", "d3"], dry_run=True, progress_cb=ticks.append
    )

    assert ticks == [1, 1, 1]  # one per drawer, including both early-outs
    assert (kept, deleted) == (["d1"], ["d2", "d3"])


def test_dedup_source_group_needs_no_callback():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1"],
        "documents": ["a document body long enough to survive"],
        "metadatas": [{}],
    }
    kept, deleted = dedup.dedup_source_group(col, ["d1"], dry_run=True)
    assert (kept, deleted) == (["d1"], [])


@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_dedup_palace_emits_per_drawer_progress_to_stderr(
    mock_backend_cls, mock_groups, tmp_path, capsys
):
    """Gap 2, outer loop: the real dedup_source_group is driven here, so this
    proves the callback dedup_palace supplies actually fires per drawer."""
    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": ["a document body long enough to survive", "another distinct body, also long"],
        "metadatas": [{}, {}],
    }
    mock_col.query.return_value = {"ids": [["d1"]], "distances": [[0.9]]}
    _install_mock_backend(mock_backend_cls, mock_col)
    mock_groups.return_value = {"a.txt": ["d1", "d2"]}

    dedup.dedup_palace(palace_path=str(tmp_path), dry_run=True, progress=True)
    captured = capsys.readouterr()

    assert "embedding dedup:" in captured.err
    assert "100.0%" in captured.err
    assert "done, 2 processed" in captured.err
    assert "1 sources" in captured.err
    assert "embedding dedup" not in captured.out


@patch("mempalace.dedup.dedup_source_group")
@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_dedup_palace_is_silent_by_default(
    mock_backend_cls, mock_groups, mock_dedup_group, tmp_path, capsys
):
    mock_col = MagicMock()
    mock_col.count.return_value = 5
    _install_mock_backend(mock_backend_cls, mock_col)
    mock_groups.return_value = {"a.txt": ["d1", "d2"]}
    mock_dedup_group.return_value = (["d1"], ["d2"])

    dedup.dedup_palace(palace_path=str(tmp_path), dry_run=True)

    assert capsys.readouterr().err == ""
    # the per-drawer callback is not even constructed when progress is off
    assert mock_dedup_group.call_args.kwargs["progress_cb"] is None


@patch("mempalace.dedup.dedup_source_group")
@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_dedup_palace_threads_progress_into_the_metadata_pass(
    mock_backend_cls, mock_groups, mock_dedup_group, tmp_path
):
    """The dry-run path must also get the Gap 1 heartbeat, not just the stats path."""
    mock_col = MagicMock()
    mock_col.count.return_value = 0
    _install_mock_backend(mock_backend_cls, mock_col)
    mock_groups.return_value = {}

    dedup.dedup_palace(palace_path=str(tmp_path), dry_run=True, progress=True)

    assert mock_groups.call_args.kwargs["progress"] is True


@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_show_stats_threads_progress_into_the_metadata_pass(
    mock_backend_cls, mock_get_groups, tmp_path
):
    _install_mock_backend(mock_backend_cls, MagicMock())
    mock_get_groups.return_value = {"a.txt": ["d1", "d2"]}

    dedup.show_stats(palace_path=str(tmp_path), progress=True)

    assert mock_get_groups.call_args.kwargs["progress"] is True


def test_progress_never_writes_to_stdout_on_any_hash_scan(capsys):
    """Requirement from #32: a progress write must not be able to corrupt a
    stdout document. Every scan is asserted, not only the newest one, and the
    two scans shipped in #33 (which printed to stdout) are included."""
    col = MagicMock()
    col.get.return_value = {"documents": ["x", "x"]}
    dedup.count_exact_duplicates(col, {"s": ["1", "2"]}, progress=True)
    captured = capsys.readouterr()
    assert "exact-duplicate scan" in captured.err
    assert captured.out == ""

    col = MagicMock()
    col.get.side_effect = _docs_by_id({"1": "x", "2": "x"})
    dedup.count_cross_source_duplicates(col, {"a": ["1"], "b": ["2"]}, progress=True)
    captured = capsys.readouterr()
    assert "cross-source duplicate scan" in captured.err
    assert captured.out == ""


def test_progress_tick_and_end_are_no_ops_without_a_total(capsys):
    """A zero-drawer scope must not divide by zero or print a fake percentage."""
    dedup._progress_tick("scan", 0, 0)
    assert capsys.readouterr().err == ""

    dedup._progress_end("scan", 0)
    assert "done, 0 processed" in capsys.readouterr().err


# ── completion metrics for on-demand runs (iMelki/mempalace#32) ───────


def test_format_metrics_renders_a_missing_measurement_as_not_computed():
    """None must never render as 0: a pass that did not run measured nothing."""
    line = dedup._format_metrics({"outcome": "ok", "sets": None, "drawers": 0})
    assert line == "outcome=ok  sets=not-computed  drawers=0"


@patch("mempalace.dedup.dedup_source_group")
@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_dedup_palace_dry_run_metrics_report_flagged_not_removed(
    mock_backend_cls, mock_groups, mock_dedup_group, tmp_path, capsys
):
    """Gap 3: the dry-run is the pass that precedes --apply, so it must state
    what it FOUND while being explicit that it changed nothing."""
    mock_col = MagicMock()
    mock_col.count.return_value = 10
    _install_mock_backend(mock_backend_cls, mock_col)
    mock_groups.return_value = {"a.txt": ["d1", "d2", "d3", "d4", "d5"]}
    mock_dedup_group.return_value = (["d1", "d2", "d3"], ["d4", "d5"])

    metrics = dedup.dedup_palace(palace_path=str(tmp_path), dry_run=True)

    assert metrics["operation"] == "dedup"
    assert metrics["outcome"] == "ok"
    assert metrics["status"] == "dry-run"
    assert metrics["sources_processed"] == 1
    assert metrics["drawers_processed"] == 5
    assert metrics["drawers_kept"] == 3
    assert metrics["drawers_flagged"] == 2
    assert metrics["drawers_removed"] == 0  # nothing was deleted
    assert metrics["palace_drawers_after"] == 10
    assert metrics["duration_seconds"] >= 0
    assert metrics["warm_seconds"] is None

    out = capsys.readouterr().out
    assert "status=dry-run" in out
    assert "drawers_flagged=2" in out
    assert "drawers_removed=0" in out
    assert "duration_seconds=" in out


def _fake_palace(monkeypatch, drawer_count=2, warm=None):
    """dedup_palace wired to fakes, with a controllable post-deletion warm."""

    class _FakeCol:
        def count(self):
            return drawer_count

    class _FakeBackend:
        def get_collection(self, *a, **k):
            return _FakeCol()

    import mempalace.searcher as searcher

    monkeypatch.setattr(searcher, "search_memories", warm or (lambda *a, **k: {"results": []}))
    monkeypatch.setattr(dedup, "ChromaBackend", lambda: _FakeBackend())
    monkeypatch.setattr(dedup, "get_source_groups", lambda *a, **k: {"src.md": ["d1", "d2"]})
    monkeypatch.setattr(dedup, "dedup_source_group", lambda *a, **k: (["d1"], ["d2"]))


def test_dedup_palace_apply_metrics_report_real_removals(monkeypatch, capsys):
    _fake_palace(monkeypatch)

    metrics = dedup.dedup_palace(palace_path="X:/fake-palace", dry_run=False)

    assert metrics["status"] == "applied"
    assert metrics["outcome"] == "ok"
    assert metrics["drawers_flagged"] == 1
    assert metrics["drawers_removed"] == 1  # --apply: flagged became removed
    assert metrics["warm_seconds"] is not None  # the post-mutation warm is timed
    assert "status=applied" in capsys.readouterr().out


def test_dedup_palace_metrics_flag_a_failed_post_mutation_warm(monkeypatch, capsys):
    """A skipped warm is not a failed dedup, but the run is not clean either:
    the next reader pays the cost this pass was meant to absorb."""

    def _boom(*a, **k):
        raise RuntimeError("palace unavailable")

    _fake_palace(monkeypatch, warm=_boom)

    metrics = dedup.dedup_palace(palace_path="X:/fake-palace", dry_run=False)

    assert metrics["outcome"] == "ok-with-warnings"
    assert metrics["drawers_removed"] == 1
    assert metrics["warm_seconds"] is None
    assert "outcome=ok-with-warnings" in capsys.readouterr().out


@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_show_stats_metadata_only_metrics_measure_nothing_honestly(
    mock_backend_cls, mock_get_groups, tmp_path, capsys
):
    _install_mock_backend(mock_backend_cls, MagicMock())
    mock_get_groups.return_value = {"a.txt": ["d1", "d2"]}

    metrics = dedup.show_stats(palace_path=str(tmp_path))

    assert metrics["operation"] == "dedup-stats"
    assert metrics["status"] == "metadata-only"
    assert metrics["sources_in_scope"] == 1
    assert metrics["drawers_in_scope"] == 2
    assert metrics["drawers_hashed"] == 0
    assert metrics["exact_duplicate_sets"] is None
    assert metrics["cross_source_duplicate_sets"] is None
    assert metrics["duration_seconds"] >= 0

    out = capsys.readouterr().out
    assert "status=metadata-only" in out
    assert "exact_duplicate_sets=not-computed" in out


@patch("mempalace.dedup.ChromaBackend")
def test_show_stats_metrics_name_every_pass_that_ran(mock_backend_cls, tmp_path, capsys):
    """Both hash passes read different drawer sets, so their counts stay separate
    and drawers_hashed is the total actually read."""
    mock_col = MagicMock()
    mock_col.get.side_effect = _docs_by_id({"d1": "same", "d2": "same", "d3": "same"})
    _install_mock_backend(mock_backend_cls, mock_col)

    groups = {"a.txt": ["d1", "d2"], "b.txt": ["d3"]}
    with patch.object(dedup, "get_source_groups", return_value=groups):
        metrics = dedup.show_stats(
            palace_path=str(tmp_path),
            min_count=2,
            exact_duplicates=True,
            cross_source_duplicates=True,
        )

    assert metrics["status"] == "exact+cross-source"
    assert metrics["exact_duplicate_sets"] == 1  # only a.txt's pair
    assert metrics["exact_redundant_drawers"] == 1
    assert metrics["cross_source_duplicate_sets"] == 1  # all three merge
    assert metrics["cross_source_redundant_drawers"] == 2
    assert metrics["drawers_hashed"] == 5  # 2 intra-source + 3 cross-source

    assert "status=exact+cross-source" in capsys.readouterr().out


# ── a redirected run must reach its metrics (iMelki/mempalace#32) ──────


def test_dedup_palace_survives_a_cp1252_stdout(monkeypatch):
    """Regression: `python -m mempalace.dedup --progress > run.log` on Windows
    redirects stdout to a cp1252 stream, and this module's own literal U+2500
    rules and U+2192 arrows are not routed through _printable(), so the run died
    with UnicodeEncodeError after the header — before a single result line and
    before the completion metrics. Found by running the real CLI with redirected
    streams, which capsys does not reproduce."""
    _fake_palace(monkeypatch)
    buf, stream = _cp1252_stdout(monkeypatch)

    metrics = dedup.dedup_palace(palace_path="X:/fake-palace", dry_run=True, progress=True)

    stream.flush()
    out = buf.getvalue().decode("cp1252")

    assert metrics["status"] == "dry-run"
    assert "src.md" in out  # the per-source line, which carries the arrow
    assert "Metrics: operation=dedup" in out  # the run reached its own end
    assert "[DRY RUN] No changes written" in out


def test_show_stats_notes_survive_a_cp1252_stdout(monkeypatch):
    """The --exact-duplicates NOTE block held a literal em dash."""
    mock_col = MagicMock()
    mock_col.get.return_value = {"documents": ["same", "same"]}
    backend = MagicMock()
    backend.get_collection.return_value = mock_col
    monkeypatch.setattr(dedup, "ChromaBackend", lambda: backend)
    monkeypatch.setattr(dedup, "get_source_groups", lambda *a, **k: {"a.txt": ["d1", "d2"]})

    buf, stream = _cp1252_stdout(monkeypatch)
    metrics = dedup.show_stats(palace_path="X:/fake-palace", exact_duplicates=True)

    stream.flush()
    out = buf.getvalue().decode("cp1252")

    assert metrics["exact_duplicate_sets"] == 1
    assert "--cross-source-duplicates for that" in out
    assert "Metrics: operation=dedup-stats" in out


def test_no_printed_literal_requires_a_non_ascii_codepage():
    """Generic guard so the crash above cannot come back through a new print.

    _printable() sanitizes the arbitrary user content this module prints, but it
    is never applied to the module's own literals, so those must be ASCII. Walks
    the AST rather than the raw text so f-string fragments are covered too.
    """
    import ast
    import io

    source = io.open(dedup.__file__, encoding="utf-8").read()
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"):
            continue
        for part in ast.walk(node):
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                if not part.value.isascii():
                    offenders.append((part.lineno, part.value))

    assert offenders == []
    assert dedup.RULE.isascii()  # the shared horizontal rule, used via a constant

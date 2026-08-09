"""Tests for mempalace.dedup — near-duplicate drawer detection and removal."""

import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

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
        r"X:\projects\ExampleOrg\Widget_System\widget-system\assets\js\bui.js",
        r"X:\projects\ExampleOrg\Widget_System\widget-system-mobile-fixes\assets\js\bui.js",
        r"X:\projects\ExampleOrg\Widget_System\widget-system-all-ui-alpha-ver\assets\js\bui.js",
        r"X:\projects\ExampleOrg\Widget_System\widget-system_backup_250522\assets\js\bui.js",
        r"X:\projects\ExampleOrg\Widget_System\files\bui.js",
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


# ── same-filename-only cross-source APPLY path (iMelki/mempalace#19) ───
#
# Operator policy decision (2026-08-07): cleanup touches ONLY duplicate sets
# where every contributing drawer shares the SAME filename. The 256
# mixed-filename shared-boilerplate sets are PERMANENTLY out of scope for
# deletion. These tests cover: the same-filename-only filter genuinely
# excludes every mixed-filename set, dry-run is the default, the
# backup-freshness gate blocks real deletion, and keep-longest selection.


# ── _parse_iso8601 ───────────────────────────────────────────────────


def test_parse_iso8601_handles_this_repos_z_suffixed_generatedat():
    """backup_snapshot._utc_now()'s own shape: seconds precision + trailing Z."""
    dt = dedup._parse_iso8601("2026-08-08T12:34:56Z")
    assert dt.year == 2026 and dt.month == 8 and dt.day == 8
    assert dt.hour == 12 and dt.minute == 34 and dt.second == 56
    assert dt.utcoffset().total_seconds() == 0


def test_parse_iso8601_handles_dotnet_seven_digit_fractional_seconds():
    """The real agent-settings backup pipeline's [DateTimeOffset]::ToString('o')
    shape -- always 7 fractional digits, explicit +00:00 offset. Python's
    fromisoformat() on the oldest CI-tested interpreter (3.9) only accepts 0,
    3, or 6 fractional digits, so this must be normalized, not passed through."""
    dt = dedup._parse_iso8601("2026-07-12T01:20:49.7912623+00:00")
    assert dt.year == 2026 and dt.month == 7 and dt.day == 12
    assert dt.hour == 1 and dt.minute == 20 and dt.second == 49


def test_parse_iso8601_handles_a_non_utc_offset():
    dt = dedup._parse_iso8601("2026-08-08T08:00:00-05:00")
    assert dt.hour == 13  # normalized to UTC
    assert dt.utcoffset().total_seconds() == 0


def test_parse_iso8601_rejects_garbage():
    import pytest

    with pytest.raises(ValueError):
        dedup._parse_iso8601("not-a-timestamp")


# ── _receipt_is_known_good ──────────────────────────────────────────


def _good_receipt(**overrides):
    """A complete, fully-verified generation receipt. Tests override the
    nested dict they care about; everything else stays known-good."""
    receipt = {
        "schema": dedup.BACKUP_RECEIPT_SCHEMA,
        "generationId": "test-generation",
        "createdAt": "2026-08-08T00:00:00Z",
        "archive": {
            "fileName": "palace-test.tar.gz",
            "lengthBytes": 1,
            "sha256": "0" * 64,
            "creationStatus": "verified",
            "structuralValidationStatus": "verified",
        },
        "offsite": {
            "status": "verified",
            "method": "cryptcheck",
            "verifiedAt": "2026-08-08T00:00:00Z",
            "objectIdentity": "gdrive:example",
        },
        "restore": {
            "status": "verified",
            "verifiedAt": "2026-08-08T00:00:00Z",
            "receiptPath": "restore.json",
            "receiptSha256": "0" * 64,
        },
        "terminal": {"status": "succeeded", "exitCode": 0},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(receipt.get(key), dict):
            receipt[key] = {**receipt[key], **value}
        else:
            receipt[key] = value
    return receipt


def test_receipt_is_known_good_accepts_a_fully_verified_receipt():
    ok, reasons = dedup._receipt_is_known_good(_good_receipt())
    assert ok is True
    assert reasons == []


def test_receipt_is_known_good_rejects_non_dict():
    ok, reasons = dedup._receipt_is_known_good(None)
    assert ok is False
    assert reasons == ["generation-receipt-unreadable"]


def test_receipt_is_known_good_rejects_wrong_schema():
    ok, reasons = dedup._receipt_is_known_good(_good_receipt(schema="something-else"))
    assert ok is False
    assert "generation-receipt-schema-mismatch" in reasons


def test_receipt_is_known_good_rejects_pending_offsite():
    """The real, current state of every archive in ~/.mempalace/backups today
    (agent-settings#457 still in progress): offsite.status == 'pending'."""
    ok, reasons = dedup._receipt_is_known_good(
        _good_receipt(offsite={"status": "pending", "method": "none"})
    )
    assert ok is False
    assert "offsite-copy-not-verified" in reasons
    assert "offsite-verification-method-insufficient" in reasons


def test_receipt_is_known_good_rejects_unverified_restore():
    ok, reasons = dedup._receipt_is_known_good(_good_receipt(restore={"status": "not-run"}))
    assert ok is False
    assert "disposable-restore-not-verified" in reasons


def test_receipt_is_known_good_rejects_failed_terminal_state():
    ok, reasons = dedup._receipt_is_known_good(
        _good_receipt(terminal={"status": "failed", "exitCode": 1})
    )
    assert ok is False
    assert "generation-terminal-state-not-success" in reasons


# ── check_backup_freshness ──────────────────────────────────────────


def _write_backup_archive(backup_dir, name, receipt=None):
    """Create <backup_dir>/<name> plus its .receipt.json (unless receipt is
    None, to test a missing-receipt archive)."""
    archive_path = backup_dir / name
    archive_path.write_bytes(b"fake-archive-bytes")
    if receipt is not None:
        (backup_dir / f"{name}.receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    return archive_path


def test_check_backup_freshness_missing_directory_fails_closed(tmp_path):
    result = dedup.check_backup_freshness(backup_dir=str(tmp_path / "does-not-exist"))
    assert result["ok"] is False
    assert "backup-directory-missing" in result["problems"]


def test_check_backup_freshness_empty_directory_fails_closed(tmp_path):
    result = dedup.check_backup_freshness(backup_dir=str(tmp_path))
    assert result["ok"] is False
    assert "no-archives-present" in result["problems"]
    assert result["archives_checked"] == 0


def test_check_backup_freshness_missing_receipt_fails_closed(tmp_path):
    _write_backup_archive(tmp_path, "palace-2026-08-08-0000.tar.gz", receipt=None)
    result = dedup.check_backup_freshness(backup_dir=str(tmp_path))
    assert result["ok"] is False
    assert result["known_good_count"] == 0
    assert any("generation-receipt-missing" in p for p in result["problems"])


def test_check_backup_freshness_unreadable_receipt_fails_closed(tmp_path):
    archive = tmp_path / "palace-2026-08-08-0000.tar.gz"
    archive.write_bytes(b"x")
    (tmp_path / f"{archive.name}.receipt.json").write_text("not json", encoding="utf-8")
    result = dedup.check_backup_freshness(backup_dir=str(tmp_path))
    assert result["ok"] is False
    assert any("generation-receipt-unreadable" in p for p in result["problems"])


def test_check_backup_freshness_stale_backup_fails_closed(tmp_path):
    """A known-good receipt older than max_age_days must still block."""
    import datetime as dt_module

    now = dt_module.datetime(2026, 8, 8, tzinfo=dt_module.timezone.utc)
    stale_created = (now - dt_module.timedelta(days=5)).isoformat()
    _write_backup_archive(
        tmp_path,
        "palace-2026-08-03.tar.gz",
        receipt=_good_receipt(createdAt=stale_created),
    )
    result = dedup.check_backup_freshness(backup_dir=str(tmp_path), max_age_days=2, now=now)
    assert result["ok"] is False
    assert any("generation-stale" in p for p in result["problems"])


def test_check_backup_freshness_future_timestamp_fails_closed(tmp_path):
    import datetime as dt_module

    now = dt_module.datetime(2026, 8, 8, tzinfo=dt_module.timezone.utc)
    future_created = (now + dt_module.timedelta(days=1)).isoformat()
    _write_backup_archive(
        tmp_path,
        "palace-2026-08-09.tar.gz",
        receipt=_good_receipt(createdAt=future_created),
    )
    result = dedup.check_backup_freshness(backup_dir=str(tmp_path), now=now)
    assert result["ok"] is False
    assert any("generation-created-at-in-future" in p for p in result["problems"])


def test_check_backup_freshness_offsite_pending_fails_closed(tmp_path):
    """Mirrors the REAL current state of this workspace's palace backups."""
    import datetime as dt_module

    now = dt_module.datetime(2026, 8, 8, tzinfo=dt_module.timezone.utc)
    fresh_created = now.isoformat()
    _write_backup_archive(
        tmp_path,
        "palace-2026-08-08.tar.gz",
        receipt=_good_receipt(
            createdAt=fresh_created, offsite={"status": "pending", "method": "none"}
        ),
    )
    result = dedup.check_backup_freshness(backup_dir=str(tmp_path), now=now)
    assert result["ok"] is False
    assert result["known_good_count"] == 0


def test_check_backup_freshness_passes_with_one_fresh_known_good_archive(tmp_path):
    import datetime as dt_module

    now = dt_module.datetime(2026, 8, 8, tzinfo=dt_module.timezone.utc)
    fresh_created = (now - dt_module.timedelta(hours=6)).isoformat()
    _write_backup_archive(
        tmp_path,
        "palace-2026-08-08.tar.gz",
        receipt=_good_receipt(createdAt=fresh_created),
    )
    result = dedup.check_backup_freshness(backup_dir=str(tmp_path), max_age_days=2, now=now)
    assert result["ok"] is True
    assert result["known_good_count"] == 1
    assert result["newest_known_good_age_days"] == pytest.approx(0.25, abs=0.01)


def test_check_backup_freshness_one_good_archive_is_enough_among_several_bad(tmp_path):
    import datetime as dt_module

    now = dt_module.datetime(2026, 8, 8, tzinfo=dt_module.timezone.utc)
    _write_backup_archive(tmp_path, "palace-a.tar.gz", receipt=None)
    _write_backup_archive(
        tmp_path,
        "palace-b.tar.gz",
        receipt=_good_receipt(createdAt=(now - dt_module.timedelta(days=10)).isoformat()),  # stale
    )
    _write_backup_archive(
        tmp_path,
        "palace-c.tar.gz",
        receipt=_good_receipt(createdAt=(now - dt_module.timedelta(hours=1)).isoformat()),
    )
    result = dedup.check_backup_freshness(backup_dir=str(tmp_path), now=now)
    assert result["ok"] is True
    assert result["known_good_count"] == 1
    assert result["archives_checked"] == 3


def test_check_backup_freshness_default_backup_dir_is_the_real_mempalace_backups_dir():
    assert dedup.DEFAULT_BACKUP_DIR == os.path.join(
        os.path.expanduser("~"), ".mempalace", "backups"
    )


def test_check_backup_freshness_refuses_against_the_real_workspace_backups_right_now():
    """Live proof, not a mock: this workspace's actual ~/.mempalace/backups
    directory, read-only. As of this task (iMelki/mempalace#19), every local
    archive's offsite.status is 'pending' -- agent-settings#457 (offsite
    backup) is still in progress -- so the gate must refuse. This test is the
    demonstration the task asked for: the precondition check correctly
    blocking a live run right now, not a bypass of it. Skips cleanly if this
    specific machine's backup directory is absent (e.g. CI), since the
    directory-missing and no-archives paths are already covered above."""
    if not os.path.isdir(dedup.DEFAULT_BACKUP_DIR):
        pytest.skip("no local ~/.mempalace/backups directory on this runner")

    result = dedup.check_backup_freshness()  # read-only; deletes nothing

    assert result["ok"] is False
    assert result["known_good_count"] == 0
    assert result["archives_checked"] > 0


# ── plan_same_filename_deletions ────────────────────────────────────


def test_plan_same_filename_deletions_excludes_every_mixed_filename_set():
    """The core operator-decision guarantee: construct a fixture with BOTH a
    same-filename cross-source set and a mixed-filename shared-boilerplate
    set, and assert only the same-filename one is EVER a deletion candidate."""
    docs = {
        # same file, three project copies -- IN scope
        "c1": "COPIED",
        "c2": "COPIED",
        "c3": "COPIED",
        # different files sharing one chunk (CSS skins) -- PERMANENTLY OUT
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

    plan = dedup.plan_same_filename_deletions(col, groups)

    assert plan["sets_count"] == 1
    assert plan["sets"][0]["filename"] == "bootstrap.css"
    assert set(plan["delete_ids"]) <= {"c1", "c2", "c3"}
    assert "s1" not in plan["delete_ids"]
    assert "s2" not in plan["delete_ids"]
    assert plan["redundant_total"] == 2


def test_plan_same_filename_deletions_excludes_single_path_sets():
    """Repeats confined to one source path are --exact-duplicates' scope, not
    this one -- the operator's 8,145-set number is cross-path only."""
    col = MagicMock()
    col.get.side_effect = _docs_by_id({"1": "dup", "2": "dup"})

    plan = dedup.plan_same_filename_deletions(col, {"a.txt": ["1", "2"]})

    assert plan["sets_count"] == 0
    assert plan["delete_ids"] == []


def test_plan_same_filename_deletions_excludes_unique_and_empty_docs():
    col = MagicMock()
    col.get.side_effect = _docs_by_id({"1": "a", "2": "b", "3": None, "4": ""})

    plan = dedup.plan_same_filename_deletions(col, {"a.txt": ["1", "2"], "b.txt": ["3", "4"]})

    assert plan["sets_count"] == 0
    assert plan["delete_ids"] == []
    assert plan["drawers_scanned"] == 4


def test_plan_same_filename_deletions_keeps_one_deterministic_drawer_per_set():
    """Keep-longest, reusing dedup_source_group()'s convention. Within an
    exact-duplicate set every entry has IDENTICAL byte length by
    construction (same hash implies same content), so the tie always falls
    to the deterministic secondary key -- lowest drawer id -- rather than
    depending on dict/set iteration order."""
    col = MagicMock()
    col.get.side_effect = _docs_by_id({"z9": "same body", "a1": "same body", "m5": "same body"})
    groups = {
        r"S:\proj\x.txt": ["z9"],
        r"S:\proj-backup\x.txt": ["a1"],
        r"S:\proj-mobile\x.txt": ["m5"],
    }

    plan = dedup.plan_same_filename_deletions(col, groups)

    assert plan["sets"][0]["keep_id"] == "a1"
    assert set(plan["sets"][0]["delete_ids"]) == {"z9", "m5"}
    # deterministic across repeated runs, not just this one
    plan_again = dedup.plan_same_filename_deletions(col, groups)
    assert plan_again["sets"][0]["keep_id"] == "a1"


def test_plan_same_filename_deletions_redundant_is_n_minus_one_per_set():
    col = MagicMock()
    col.get.side_effect = _docs_by_id({f"d{i}": "same" for i in range(5)})
    groups = {f"S:\\proj{i}\\x.txt": [f"d{i}"] for i in range(5)}

    plan = dedup.plan_same_filename_deletions(col, groups)

    assert plan["sets_count"] == 1
    assert plan["sets"][0]["redundant"] == 4
    assert len(plan["sets"][0]["delete_ids"]) == 4
    assert plan["redundant_total"] == 4


def test_plan_same_filename_deletions_reports_contributing_paths_and_filename():
    paths = [
        r"X:\projects\ExampleOrg\Widget_System\widget-system\assets\js\bui.js",
        r"X:\projects\ExampleOrg\Widget_System\widget-system-mobile-fixes\assets\js\bui.js",
        r"X:\projects\ExampleOrg\Widget_System\widget-system_backup_250522\assets\js\bui.js",
    ]
    groups = {p: [f"d{i}"] for i, p in enumerate(paths)}
    col = MagicMock()
    col.get.side_effect = _docs_by_id({f"d{i}": "identical asset body" for i in range(3)})

    plan = dedup.plan_same_filename_deletions(col, groups)

    assert plan["sets"][0]["filename"] == "bui.js"
    assert plan["sets"][0]["paths"] == sorted(paths)
    assert plan["sets"][0]["distinct_paths"] == 3


def test_plan_same_filename_deletions_batches_and_never_reads_whole_corpus():
    ids = [f"d{i}" for i in range(1200)]
    col = MagicMock()
    col.get.side_effect = _docs_by_id({i: "same-everywhere" for i in ids})

    plan = dedup.plan_same_filename_deletions(col, {"a.txt": ids[:600], "b.txt": ids[600:]})

    sizes = [len(call.kwargs["ids"]) for call in col.get.call_args_list]
    assert max(sizes) <= dedup.HASH_BATCH_SIZE
    assert plan["drawers_scanned"] == 1200


def test_plan_same_filename_deletions_emits_progress(capsys):
    col = MagicMock()
    col.get.side_effect = _docs_by_id({"1": "x", "2": "x"})

    dedup.plan_same_filename_deletions(col, {"a.txt": ["1"], "b.txt": ["2"]}, progress=True)
    captured = capsys.readouterr()

    assert "same-filename plan scan:" in captured.err
    assert "same-filename plan scan" not in captured.out


def test_plan_same_filename_deletions_never_calls_delete():
    """Planning is read-only; deletion happens only in apply_same_filename_dedup
    after the backup gate passes, never inside the planner itself."""
    col = MagicMock()
    col.get.side_effect = _docs_by_id({"1": "same", "2": "same"})

    dedup.plan_same_filename_deletions(col, {"a": ["1"], "b": ["2"]})

    col.delete.assert_not_called()
    col.update.assert_not_called()
    col.upsert.assert_not_called()


def test_plan_same_filename_deletions_has_no_scope_widening_parameter():
    """Structural guarantee: there is no 'force' or 'include mixed filenames'
    switch anywhere on the planning or apply functions that could reach a
    mixed-filename set, even deliberately."""
    import inspect

    for fn in (dedup.plan_same_filename_deletions, dedup.apply_same_filename_dedup):
        params = set(inspect.signature(fn).parameters)
        assert "force" not in params
        assert not any("mixed" in p or "bypass" in p or "unsafe" in p for p in params)


def test_plan_same_filename_deletions_source_always_filters_by_filename_count():
    """The filter is unconditional in the source, not gated behind a flag."""
    import inspect

    src = inspect.getsource(dedup.plan_same_filename_deletions)
    assert "len(distinct_names) != 1" in src
    assert "len(distinct_paths) < 2" in src


# ── print_same_filename_plan ────────────────────────────────────────


def test_print_same_filename_plan_shows_counts_filenames_and_keep_id(capsys):
    plan = {
        "drawers_scanned": 10,
        "sets_count": 1,
        "redundant_total": 2,
        "sets": [
            {
                "filename": "bui.js",
                "digest": "abc",
                "keep_id": "d1",
                "delete_ids": ["d2", "d3"],
                "redundant": 2,
                "distinct_paths": 3,
                "paths": ["S:\\a\\bui.js", "S:\\b\\bui.js", "S:\\c\\bui.js"],
            }
        ],
    }
    dedup.print_same_filename_plan(plan)
    out = capsys.readouterr().out

    assert "Same-filename cross-source duplicate sets: 1" in out
    assert "Redundant drawers (deletion candidates):    2" in out
    assert "bui.js" in out
    assert "keep=d1" in out
    assert "S:\\a\\bui.js" in out
    assert "PERMANENTLY out of scope" in out


def test_print_same_filename_plan_truncates_long_path_lists(capsys):
    paths = [f"S:\\proj{i}\\x.txt" for i in range(20)]
    plan = {
        "drawers_scanned": 20,
        "sets_count": 1,
        "redundant_total": 19,
        "sets": [
            {
                "filename": "x.txt",
                "digest": "abc",
                "keep_id": "d0",
                "delete_ids": [f"d{i}" for i in range(1, 20)],
                "redundant": 19,
                "distinct_paths": 20,
                "paths": paths,
            }
        ],
    }
    dedup.print_same_filename_plan(plan, max_paths=8)
    out = capsys.readouterr().out
    assert "... +12 more source path(s)" in out


def test_print_same_filename_plan_handles_empty_plan(capsys):
    dedup.print_same_filename_plan(
        {"drawers_scanned": 0, "sets_count": 0, "redundant_total": 0, "sets": []}
    )
    out = capsys.readouterr().out
    assert "Same-filename cross-source duplicate sets: 0" in out


# ── apply_same_filename_dedup ───────────────────────────────────────


def _fake_same_filename_palace(monkeypatch, groups, docs, count_sequence=None):
    """Wires get_source_groups + a fake collection whose get()/count()/
    delete() feed plan_same_filename_deletions and apply_same_filename_dedup
    without touching a real palace."""
    monkeypatch.setattr(dedup, "get_source_groups", lambda *a, **k: groups)

    counts = iter(count_sequence) if count_sequence is not None else None

    class _FakeCol:
        def __init__(self):
            self.deleted_ids = []

        def get(self, ids=None, include=None, **kwargs):
            return {"ids": list(ids), "documents": [docs[i] for i in ids]}

        def count(self):
            return next(counts) if counts is not None else len(docs)

        def delete(self, ids):
            self.deleted_ids.extend(ids)

    fake_col = _FakeCol()

    class _FakeBackend:
        def get_collection(self, *a, **k):
            return fake_col

    monkeypatch.setattr(dedup, "ChromaBackend", lambda: _FakeBackend())
    return fake_col


def test_apply_same_filename_dry_run_never_deletes(monkeypatch, capsys):
    docs = {"c1": "COPIED", "c2": "COPIED"}
    groups = {r"S:\a\x.txt": ["c1"], r"S:\b\x.txt": ["c2"]}
    fake_col = _fake_same_filename_palace(monkeypatch, groups, docs)

    metrics = dedup.apply_same_filename_dedup(palace_path="X:/fake", dry_run=True)

    assert fake_col.deleted_ids == []
    assert metrics["drawers_removed"] == 0
    assert metrics["status"] == "dry-run"
    out = capsys.readouterr().out
    assert "[DRY RUN]" in out


def test_apply_same_filename_dry_run_is_the_default_via_cli():
    """Mirrors test_cli_bare_invocation_defaults_to_dry_run: dry_run=not
    args.apply_same_filename must be the literal wiring, so a bare
    --same-filename-cleanup invocation can never delete."""
    src = open("mempalace/dedup.py", encoding="utf-8").read()
    assert "dry_run=not args.apply_same_filename" in src


def test_apply_same_filename_blocked_without_fresh_backup(monkeypatch, capsys):
    """The precondition check must refuse live deletion when no known-good
    backup exists -- proving the gate works, not working around it."""
    docs = {"c1": "COPIED", "c2": "COPIED"}
    groups = {r"S:\a\x.txt": ["c1"], r"S:\b\x.txt": ["c2"]}
    fake_col = _fake_same_filename_palace(monkeypatch, groups, docs)
    monkeypatch.setattr(
        dedup,
        "check_backup_freshness",
        lambda **k: {
            "ok": False,
            "reason": "no known-good backup archive within 2 day(s)",
            "problems": ["palace-x.tar.gz: offsite-copy-not-verified"],
        },
    )

    metrics = dedup.apply_same_filename_dedup(palace_path="X:/fake", dry_run=False)

    assert fake_col.deleted_ids == []
    assert metrics["drawers_removed"] == 0
    assert metrics["outcome"] == "blocked-backup-not-fresh"
    assert metrics["backup_gate_ok"] is False
    out = capsys.readouterr().out
    assert "[BLOCKED]" in out
    assert "offsite-copy-not-verified" in out


def test_apply_same_filename_deletes_only_same_filename_ids_when_gate_passes(monkeypatch, capsys):
    docs = {
        "c1": "COPIED",
        "c2": "COPIED",
        "s1": "SHARED",
        "s2": "SHARED",
    }
    groups = {
        r"S:\a\bootstrap.css": ["c1"],
        r"S:\b\bootstrap.css": ["c2"],
        r"S:\a\aero.css": ["s1"],
        r"S:\a\black.css": ["s2"],
    }
    fake_col = _fake_same_filename_palace(monkeypatch, groups, docs)
    monkeypatch.setattr(
        dedup,
        "check_backup_freshness",
        lambda **k: {"ok": True, "reason": "1 known-good archive", "problems": []},
    )
    monkeypatch.setattr("mempalace.searcher.search_memories", lambda *a, **k: {"results": []})

    metrics = dedup.apply_same_filename_dedup(palace_path="X:/fake", dry_run=False)

    # exactly one of c1/c2 deleted (the other kept); neither shared-boilerplate
    # id (s1, s2) is ever touched
    assert len(fake_col.deleted_ids) == 1
    assert fake_col.deleted_ids[0] in {"c1", "c2"}
    assert "s1" not in fake_col.deleted_ids
    assert "s2" not in fake_col.deleted_ids
    assert metrics["drawers_removed"] == 1
    assert metrics["outcome"] == "ok"
    assert metrics["backup_gate_ok"] is True


def test_apply_same_filename_prewarms_after_live_deletion(monkeypatch, capsys):
    docs = {"c1": "COPIED", "c2": "COPIED"}
    groups = {r"S:\a\x.txt": ["c1"], r"S:\b\x.txt": ["c2"]}
    _fake_same_filename_palace(monkeypatch, groups, docs)
    monkeypatch.setattr(
        dedup,
        "check_backup_freshness",
        lambda **k: {"ok": True, "reason": "fresh", "problems": []},
    )
    calls = []
    monkeypatch.setattr(
        "mempalace.searcher.search_memories",
        lambda *a, **k: calls.append((a, k)) or {"results": []},
    )

    dedup.apply_same_filename_dedup(palace_path="X:/fake", dry_run=False)

    out = capsys.readouterr().out
    assert "Pre-warming palace" in out
    assert calls, "post-deletion warm did not invoke search_memories"


def test_apply_same_filename_no_op_when_nothing_to_delete(monkeypatch, capsys):
    """No same-filename sets in scope: no backup check needed, no deletion,
    a clean no-op rather than a spurious block."""
    docs = {"s1": "SHARED", "s2": "SHARED"}
    groups = {r"S:\a\aero.css": ["s1"], r"S:\a\black.css": ["s2"]}
    fake_col = _fake_same_filename_palace(monkeypatch, groups, docs)
    backup_check_called = []
    monkeypatch.setattr(
        dedup,
        "check_backup_freshness",
        lambda **k: backup_check_called.append(1) or {"ok": True, "reason": "x", "problems": []},
    )

    metrics = dedup.apply_same_filename_dedup(palace_path="X:/fake", dry_run=False)

    assert fake_col.deleted_ids == []
    assert metrics["status"] == "no-op"
    assert not backup_check_called  # never even asked -- nothing to delete


# ── CLI wiring for --same-filename-cleanup / --apply-same-filename ─────


def test_cli_apply_same_filename_requires_same_filename_cleanup():
    import subprocess
    import sys as sys_module

    res = subprocess.run(
        [sys_module.executable, "-m", "mempalace.dedup", "--apply-same-filename"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 2
    assert "--apply-same-filename only applies to --same-filename-cleanup" in res.stderr


def test_cli_same_filename_cleanup_rejects_stats_combo():
    import subprocess
    import sys as sys_module

    res = subprocess.run(
        [sys_module.executable, "-m", "mempalace.dedup", "--same-filename-cleanup", "--stats"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 2
    assert "--same-filename-cleanup cannot be combined with --stats" in res.stderr


def test_cli_same_filename_cleanup_rejects_apply_combo():
    import subprocess
    import sys as sys_module

    res = subprocess.run(
        [sys_module.executable, "-m", "mempalace.dedup", "--same-filename-cleanup", "--apply"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 2
    assert "use --apply-same-filename" in res.stderr


def test_cli_same_filename_cleanup_dry_run_against_nonexistent_palace_does_not_crash_parser():
    """Parser-level wiring only: --same-filename-cleanup alone (no --apply)
    must parse cleanly (real palace connection failure is a runtime error,
    not a parser error) -- proving it is accepted as its own mode."""
    import subprocess
    import sys as sys_module

    res = subprocess.run(
        [
            sys_module.executable,
            "-m",
            "mempalace.dedup",
            "--same-filename-cleanup",
            "--palace",
            "X:/definitely-not-a-real-palace-path",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Never a parser error (exit 2); it fails later trying to open the palace.
    assert res.returncode != 2

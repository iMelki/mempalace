"""Tests for mempalace.dedup — near-duplicate drawer detection and removal."""

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
    assert "40" not in out.replace("40 ", "")  # no 0.4-derived figure (100 * 0.4)
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
    mock_groups.assert_called_once_with(mock_col, 5, None, wing="test_wing")


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

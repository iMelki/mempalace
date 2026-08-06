"""Tests for mempalace.mine_exclusions — mine-time exclusions (#36).

The issue this covers: the palace held five on-disk copies of one project
and ~6,900+ drawers of generated ``pnpm-lock.yaml`` content because those
files existed on disk and mining ingested all of them. Two behaviours are
asserted here and they pull in opposite directions on purpose:

* generated/vendored content must be **declined at ingest** (cheap, safe,
  reversible from config), and
* backup/variant directories must be **reported and never auto-excluded**
  (a directory named ``backup`` can hold the only copy of something).
"""

import argparse
import io
import json
from contextlib import redirect_stdout

import pytest
import yaml

from mempalace.cli import cmd_exclusions, cmd_variants
from mempalace.mine_exclusions import (
    DEFAULT_GENERATED_DIRS,
    DEFAULT_GENERATED_FILES,
    REASON_CONFIGURED_DIR,
    REASON_CONFIGURED_FILE,
    REASON_GENERATED_DIR,
    REASON_GENERATED_FILE,
    REASON_PROJECT_ARTIFACT,
    VARIANT_DATE_SUFFIX_RE,
    detect_variant_directories,
    format_variant_report,
    load_exclusion_policy,
    load_variant_settings,
    read_project_config,
)
from mempalace.miner import (
    SKIP_FILENAMES,
    _project_run_config,
    _source_plan_contract,
    _variant_advisory_lines,
    default_exclusion_policy,
    resolve_mine_exclusion_policy,
    scan_project,
    should_skip_dir,
)


def write(path, content=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_project_config(root, config):
    write(root / "mempalace.yaml", yaml.safe_dump(config, sort_keys=False))


def scanned(root, **kwargs):
    return sorted(path.relative_to(root).as_posix() for path in scan_project(str(root), **kwargs))


# ── default exclusion set ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename",
    [
        "pnpm-lock.yaml",
        "package-lock.json",
        "yarn.lock",
        "poetry.lock",
        "Cargo.lock",
        "composer.lock",
    ],
)
def test_default_policy_excludes_the_issue_36_lockfiles(filename):
    """The exact minimum list named in mempalace#36."""
    policy = default_exclusion_policy()
    assert policy.excludes_file(filename), f"{filename} must not be ingested by default"


@pytest.mark.parametrize(
    "dirname",
    [
        "node_modules",
        "obj",
        "bin",
        "dist",
        "build",
        ".next",
        "__pycache__",
        ".venv",
        "venv",
        "target",
    ],
)
def test_default_policy_excludes_the_issue_36_directories(dirname):
    policy = default_exclusion_policy()
    assert policy.excludes_dir(dirname), f"{dirname}/ must not be walked by default"
    assert should_skip_dir(dirname) is True


def test_default_directory_set_is_a_superset_of_the_previous_hardcoded_set():
    """No directory that was skipped before this change becomes mineable."""
    from mempalace.palace import SKIP_DIRS

    policy = default_exclusion_policy()
    for dirname in SKIP_DIRS:
        assert policy.excludes_dir(dirname), dirname
    assert policy.excludes_dir("mempalace.egg-info"), "*.egg-info glob regressed"


def test_mempalace_own_artifacts_stay_excluded():
    policy = default_exclusion_policy()
    for name in SKIP_FILENAMES:
        assert policy.excludes_file(name), name


def test_reason_codes_are_attributed():
    policy = load_exclusion_policy(
        {"exclude": {"files": ["notes.tmp"], "dirs": ["fixtures_huge"]}},
        artifact_files=SKIP_FILENAMES,
    )
    assert policy.file_exclusion_reason("pnpm-lock.yaml") == REASON_GENERATED_FILE
    assert policy.file_exclusion_reason("notes.tmp") == REASON_CONFIGURED_FILE
    assert policy.file_exclusion_reason("mempalace.yaml") == REASON_PROJECT_ARTIFACT
    assert policy.dir_exclusion_reason("node_modules") == REASON_GENERATED_DIR
    assert policy.dir_exclusion_reason("fixtures_huge") == REASON_CONFIGURED_DIR
    assert policy.file_exclusion_reason("app.py") is None
    assert policy.dir_exclusion_reason("src") is None


def test_matching_is_case_insensitive():
    """Windows filesystems are case-insensitive; `CARGO.LOCK` is the same file."""
    policy = default_exclusion_policy()
    assert policy.excludes_file("CARGO.LOCK")
    assert policy.excludes_file("PNPM-Lock.YAML")
    assert policy.excludes_dir("Node_Modules")
    assert policy.excludes_dir("OBJ")


def test_glob_patterns_cover_minified_bundles():
    policy = default_exclusion_policy()
    assert policy.excludes_file("vendor.min.js")
    assert policy.excludes_file("app.bundle.js")
    assert not policy.excludes_file("app.js")


def test_default_sets_are_data_not_code():
    """The lists must be module-level data so they can be audited and extended."""
    assert isinstance(DEFAULT_GENERATED_FILES, tuple)
    assert isinstance(DEFAULT_GENERATED_DIRS, tuple)
    assert "pnpm-lock.yaml" in {value.lower() for value in DEFAULT_GENERATED_FILES}
    assert "obj" in {value.lower() for value in DEFAULT_GENERATED_DIRS}


# ── reversibility: the operator's escape hatches ───────────────────────


def test_allow_files_reverses_a_lockfile_exclusion():
    """The lockfile policy call is the operator's; reversal is one config line."""
    policy = load_exclusion_policy({"exclude": {"allow_files": ["pnpm-lock.yaml"]}})
    assert not policy.excludes_file("pnpm-lock.yaml")
    assert policy.excludes_file("yarn.lock"), "allow-listing one file must not disable the set"


def test_allow_dirs_reverses_bin_for_unix_style_repos():
    policy = load_exclusion_policy({"exclude": {"allow_dirs": ["bin"]}})
    assert not policy.excludes_dir("bin")
    assert policy.excludes_dir("obj")


def test_master_switches_drop_a_whole_default_set():
    policy = load_exclusion_policy(
        {"exclude": {"generated_files": False, "generated_dirs": False}},
        artifact_files=SKIP_FILENAMES,
    )
    assert not policy.excludes_file("pnpm-lock.yaml")
    assert not policy.excludes_dir("node_modules")
    assert policy.excludes_file("mempalace.yaml"), "own artifacts are not part of the switch"


def test_allow_list_cannot_resurrect_mempalace_artifacts():
    """`allow_files` is a recall hatch, not a way to break init idempotency."""
    policy = load_exclusion_policy(
        {"exclude": {"allow_files": ["mempalace.yaml", "entities.json"]}},
        artifact_files=SKIP_FILENAMES,
    )
    assert policy.excludes_file("mempalace.yaml")
    assert policy.excludes_file("entities.json")


def test_unknown_config_keys_are_ignored():
    policy = load_exclusion_policy({"exclude": {"future_key": ["x"]}})
    assert policy.excludes_file("pnpm-lock.yaml")


@pytest.mark.parametrize("block", [None, "not-a-mapping", 42, []])
def test_malformed_exclude_block_falls_back_to_defaults(block):
    policy = load_exclusion_policy({"exclude": block})
    assert policy.excludes_file("pnpm-lock.yaml")


def test_string_booleans_from_yaml_are_honoured():
    policy = load_exclusion_policy({"exclude": {"generated_files": "false"}})
    assert not policy.excludes_file("pnpm-lock.yaml")


# ── policy digest ──────────────────────────────────────────────────────


def test_policy_digest_is_stable_and_sensitive():
    baseline = load_exclusion_policy(None)
    assert baseline.digest() == load_exclusion_policy(None).digest()
    changed = load_exclusion_policy({"exclude": {"allow_files": ["pnpm-lock.yaml"]}})
    assert changed.digest() != baseline.digest()
    assert baseline.digest().startswith("sha256:")


def test_plan_contract_binds_the_exclusion_policy_digest():
    """A plan built under one policy must not be resumable under another."""
    run_config = _project_run_config(
        wing="w",
        rooms=[{"name": "general"}],
        respect_gitignore=True,
        include_ignored=[],
        limit=0,
    )
    default_contract = _source_plan_contract(run_config)
    relaxed = load_exclusion_policy({"exclude": {"generated_files": False}})
    relaxed_contract = _source_plan_contract(run_config, exclusion_policy=relaxed)

    assert "exclusion_policy_digest" in default_contract
    assert (
        default_contract["exclusion_policy_digest"] != (relaxed_contract["exclusion_policy_digest"])
    )
    # The receipt config digest is deliberately NOT affected: exclusions only
    # remove future sources, so already-correct receipts stay valid.
    assert default_contract["receipt_config_digest"] == relaxed_contract["receipt_config_digest"]


# ── scan_project integration: the actual regression ────────────────────


def test_scan_project_declines_lockfiles_and_build_output(tmp_path):
    write(tmp_path / "src" / "app.py", "print('x')\n")
    write(tmp_path / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n" * 500)
    write(tmp_path / "yarn.lock", "# yarn lockfile v1\n")
    write(tmp_path / "obj" / "Debug" / "manifest.json", '{"generated": true}')
    write(tmp_path / "node_modules" / "left-pad" / "index.js", "module.exports = 1")
    write(tmp_path / "docs" / "notes.md", "# real content\n")

    assert scanned(tmp_path) == ["docs/notes.md", "src/app.py"]


def test_scan_project_honours_a_project_allow_list(tmp_path):
    write(tmp_path / "src" / "app.py", "print('x')\n")
    write(tmp_path / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    write_project_config(
        tmp_path,
        {
            "wing": "demo",
            "rooms": [{"name": "general", "description": "all"}],
            "exclude": {"allow_files": ["pnpm-lock.yaml"]},
        },
    )

    assert scanned(tmp_path) == ["pnpm-lock.yaml", "src/app.py"]


def test_scan_project_honours_extra_configured_exclusions(tmp_path):
    write(tmp_path / "src" / "app.py", "print('x')\n")
    write(tmp_path / "fixtures_huge" / "dump.json", "{}")
    write(tmp_path / "generated.report.json", "{}")
    write_project_config(
        tmp_path,
        {
            "wing": "demo",
            "rooms": [{"name": "general", "description": "all"}],
            "exclude": {"dirs": ["fixtures_huge"], "files": ["*.report.json"]},
        },
    )

    assert scanned(tmp_path) == ["src/app.py"]


def test_include_ignored_still_force_includes_an_excluded_lockfile(tmp_path):
    """The pre-existing per-path override keeps working over the new policy."""
    write(tmp_path / "src" / "app.py", "print('x')\n")
    write(tmp_path / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")

    assert scanned(tmp_path, include_ignored=["pnpm-lock.yaml"]) == [
        "pnpm-lock.yaml",
        "src/app.py",
    ]


def test_backup_directories_are_still_mined(tmp_path):
    """The safety invariant: detection reports, it never excludes.

    A directory named ``*_backup_250522`` can hold the only surviving copy of
    something. Its content must still be scanned.
    """
    write(tmp_path / "repeater-system" / "assets" / "bui.js", "var a = 1;\n")
    write(tmp_path / "repeater-system_backup_250522" / "assets" / "bui.js", "var a = 1;\n")
    write(tmp_path / "backend-backup-git_broke" / "server.py", "print('x')\n")

    found = scanned(tmp_path)
    assert "repeater-system_backup_250522/assets/bui.js" in found
    assert "backend-backup-git_broke/server.py" in found


# ── variant detection (report-only) ────────────────────────────────────


def test_detects_the_issue_36_variant_layout(tmp_path):
    for name in (
        "repeater-system",
        "repeater-system-mobile-fixes",
        "repeater-system-all-ui-alpha-ver",
        "repeater-system_backup_250522",
    ):
        (tmp_path / name / "assets").mkdir(parents=True)

    candidates = detect_variant_directories(str(tmp_path))
    by_name = {candidate.name: candidate for candidate in candidates}

    assert "repeater-system" not in by_name, "the original must not be flagged"
    assert set(by_name) == {
        "repeater-system-mobile-fixes",
        "repeater-system-all-ui-alpha-ver",
        "repeater-system_backup_250522",
    }
    backup = by_name["repeater-system_backup_250522"]
    assert backup.confidence == "high"
    assert backup.sibling_base == "repeater-system"
    assert "date-stamp" in backup.signals
    assert "sibling-prefix" in backup.signals
    assert "name-pattern" in backup.signals


def test_detects_a_backup_sibling_of_a_backend_directory(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend-backup-git_broke").mkdir()

    candidates = detect_variant_directories(str(tmp_path))
    assert [candidate.name for candidate in candidates] == ["backend-backup-git_broke"]
    assert candidates[0].sibling_base == "backend"
    assert candidates[0].confidence == "high"


def test_ordinary_project_siblings_are_not_flagged(tmp_path):
    for name in ("src", "tests", "docs", "scripts", "server", "client"):
        (tmp_path / name).mkdir()

    assert detect_variant_directories(str(tmp_path)) == []


def test_short_stems_do_not_imply_a_copy(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app-server").mkdir()

    assert detect_variant_directories(str(tmp_path)) == []


def test_excluded_directories_are_pruned_from_detection(tmp_path):
    (tmp_path / "node_modules" / "pkg-backup").mkdir(parents=True)
    (tmp_path / "site").mkdir()

    assert detect_variant_directories(str(tmp_path)) == []


def test_detection_respects_max_depth(tmp_path):
    (tmp_path / "a" / "b" / "project_backup").mkdir(parents=True)

    assert detect_variant_directories(str(tmp_path), max_depth=2) == []
    deep = detect_variant_directories(str(tmp_path), max_depth=3)
    assert [candidate.relative_path for candidate in deep] == ["a/b/project_backup"]


def test_detection_on_a_missing_directory_is_empty(tmp_path):
    assert detect_variant_directories(str(tmp_path / "nope")) == []


@pytest.mark.parametrize(
    "name,expected",
    [
        ("repeater-system_backup_250522", True),
        ("release-20250522", True),
        ("notes.250522", True),
        ("api-v3", False),
        ("bui2", False),
        ("build-259999", False),
        ("thing-251301", False),
    ],
)
def test_date_stamp_regex(name, expected):
    assert bool(VARIANT_DATE_SUFFIX_RE.search(name)) is expected


def test_extra_variant_globs_come_from_config():
    settings = load_variant_settings({"variants": {"globs": ["*_snapshot"]}})
    assert "*_snapshot" in settings["globs"]
    assert "*backup*" in settings["globs"], "defaults must still apply"


def test_variant_settings_defaults_and_coercion():
    assert load_variant_settings(None) == load_variant_settings({})
    assert load_variant_settings({"variants": {"max_depth": "bad"}})["max_depth"] == 3
    assert load_variant_settings({"variants": {"max_depth": 0}})["max_depth"] == 1
    assert load_variant_settings({"variants": {"enabled": False}})["enabled"] is False


def test_report_states_plainly_that_nothing_was_excluded(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend-backup").mkdir()

    text = "\n".join(format_variant_report(detect_variant_directories(str(tmp_path))))
    assert "REPORT ONLY" in text
    assert "nothing was excluded" in text
    assert "exclude.dirs" in text


def test_empty_report_says_none():
    assert "none detected" in format_variant_report([])[0]


def test_report_truncates_to_the_limit(tmp_path):
    (tmp_path / "base-project").mkdir()
    for index in range(6):
        (tmp_path / f"base-project_backup_{index}").mkdir()

    lines = format_variant_report(detect_variant_directories(str(tmp_path)), limit=2)
    assert any("4 more" in line for line in lines)


# ── mine-header advisory ───────────────────────────────────────────────


def test_mine_advisory_reports_candidates(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend-backup-git_broke").mkdir()

    lines = _variant_advisory_lines(str(tmp_path))
    assert any("backend-backup-git_broke" in line for line in lines)


def test_mine_advisory_can_be_disabled(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend-backup").mkdir()

    assert _variant_advisory_lines(str(tmp_path), enabled=False) == []
    assert _variant_advisory_lines(str(tmp_path), {"variants": {"enabled": False}}) == []


def test_mine_advisory_never_raises(tmp_path):
    """An advisory that could abort an ingest is worse than no advisory."""
    assert _variant_advisory_lines(str(tmp_path / "missing")) == []
    assert _variant_advisory_lines(str(tmp_path), {"variants": "broken"}) == []


# ── project config reading ─────────────────────────────────────────────


def test_read_project_config_tolerates_a_malformed_yaml(tmp_path):
    write(tmp_path / "mempalace.yaml", "wing: [unclosed\n")
    assert read_project_config(str(tmp_path)) == {}


def test_read_project_config_tolerates_a_missing_yaml(tmp_path):
    assert read_project_config(str(tmp_path)) == {}


def test_read_project_config_accepts_the_legacy_name(tmp_path):
    write(tmp_path / "mempal.yaml", yaml.safe_dump({"exclude": {"allow_dirs": ["bin"]}}))
    policy = resolve_mine_exclusion_policy(str(tmp_path))
    assert not policy.excludes_dir("bin")


# ── CLI surfaces ───────────────────────────────────────────────────────


def test_cmd_variants_json_marks_itself_as_not_applied(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend-backup-git_broke").mkdir()

    buffer = io.StringIO()
    args = argparse.Namespace(dir=str(tmp_path), max_depth=None, json=True)
    with redirect_stdout(buffer):
        cmd_variants(args)
    payload = json.loads(buffer.getvalue())

    assert payload["applied"] is False
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["name"] == "backend-backup-git_broke"


def test_cmd_variants_rejects_a_missing_directory(tmp_path):
    args = argparse.Namespace(dir=str(tmp_path / "nope"), max_depth=None, json=False)
    with pytest.raises(SystemExit) as excinfo:
        cmd_variants(args)
    assert excinfo.value.code == 2


def test_cmd_exclusions_json_reports_the_effective_policy(tmp_path):
    write_project_config(
        tmp_path,
        {"wing": "demo", "exclude": {"allow_files": ["pnpm-lock.yaml"]}},
    )
    buffer = io.StringIO()
    args = argparse.Namespace(dir=str(tmp_path), json=True)
    with redirect_stdout(buffer):
        cmd_exclusions(args)
    payload = json.loads(buffer.getvalue())

    assert payload["configured"] is True
    assert payload["policy"]["allow_files"] == ["pnpm-lock.yaml"]
    assert payload["digest"].startswith("sha256:")


def test_cmd_exclusions_default_output_is_ascii(tmp_path):
    """cp1252 consoles must not lose the report (mirrors the dedup --stats fix)."""
    buffer = io.StringIO()
    args = argparse.Namespace(dir=str(tmp_path), json=False)
    with redirect_stdout(buffer):
        cmd_exclusions(args)
    buffer.getvalue().encode("cp1252")

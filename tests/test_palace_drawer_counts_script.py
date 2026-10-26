"""Tests for the SQLite query embedded in scripts/Get-PalaceDrawerCounts.ps1.

The PowerShell wrapper is not run here (CI has no pwsh). The embedded Python is
extracted and executed against a throwaway chroma-shaped database instead, and
the wrapper is checked for the constructs the review findings were about.
"""

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "Get-PalaceDrawerCounts.ps1"


def _embedded_python() -> str:
    match = re.search(r"@'\r?\n(.*?)\r?\n'@", SCRIPT.read_text(encoding="utf-8"), re.DOTALL)
    assert match, "embedded python here-string not found"
    return match.group(1)


def _make_palace_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE collections (id TEXT, name TEXT);
        CREATE TABLE segments (id TEXT, collection TEXT, scope TEXT);
        CREATE TABLE embeddings (id INTEGER, segment_id TEXT);
        CREATE TABLE embedding_metadata (id INTEGER, key TEXT, string_value TEXT);
        INSERT INTO collections VALUES ('c1', 'mempalace_drawers');
        INSERT INTO segments VALUES ('s1', 'c1', 'METADATA');
        INSERT INTO embeddings VALUES (1, 's1'), (2, 's1'), (3, 's1');
        INSERT INTO embedding_metadata VALUES
            (1, 'wing', 'coding'), (2, 'wing', 'coding'), (3, 'wing', 'trading');
        """
    )
    conn.commit()
    conn.close()


def _run_query(tmp_path: Path, db_path: Path) -> subprocess.CompletedProcess:
    runner = tmp_path / "query.py"
    runner.write_text(_embedded_python(), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(runner), str(db_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("dirname", ["palace", "palace#archive", "palace%20x", "what?ever"])
def test_query_reads_the_real_database_for_special_characters_in_path(tmp_path, dirname):
    if sys.platform == "win32" and "?" in dirname:
        pytest.skip("'?' is not a valid Windows path character")
    db_path = tmp_path / dirname / "chroma.sqlite3"
    _make_palace_db(db_path)
    before = {p.name for p in tmp_path.iterdir()}

    result = _run_query(tmp_path, db_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"coding": 2, "trading": 1}
    # A truncated URI used to create an unintended sibling database named "palace".
    assert {p.name for p in tmp_path.iterdir()} - before == {"query.py"}


def test_query_fails_instead_of_creating_a_missing_database(tmp_path):
    result = _run_query(tmp_path, tmp_path / "absent#dir" / "chroma.sqlite3")

    assert result.returncode != 0
    assert not (tmp_path / "absent").exists()
    assert not (tmp_path / "absent#dir").exists()


def test_wrapper_avoids_powershell_6_only_json_switch():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "-AsHashtable" not in source


def test_wrapper_warns_when_an_existing_database_cannot_be_queried():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "Write-Warning" in source
    assert "catch { }" not in source

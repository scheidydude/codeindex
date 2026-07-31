# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
"""Phase 1 acceptance tests.

Acceptance criteria (from CKG-DESIGN-001):
  1. Exported JSON is semantically identical to pre-change output on a fixture
     repo (golden test).
  2. Re-running analyze after editing one file logs only that file's change
     set (incremental detection test).
  3. `blastradius db status` reports correct counts.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Fixture repo checked into tests/fixtures/simple_python/
FIXTURE_SRC = Path(__file__).parent / "fixtures" / "simple_python"


def _init_git(repo: Path) -> str:
    """Initialise a git repo and commit all files; return HEAD hash."""
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True,
                   capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _run_analyze(repo: Path) -> dict:
    """Run blastradius.index.build() and return the parsed JSON output."""
    # Import here so we get the Phase-1 version, not a cached import
    import importlib
    import blastradius.index as idx_mod
    importlib.reload(idx_mod)

    data = idx_mod.build(str(repo))
    return data


def _normalize(data: dict) -> dict:
    """Sort nodes and links for stable comparison."""
    data = json.loads(json.dumps(data))  # deep copy via JSON round-trip
    data["nodes"] = sorted(data["nodes"], key=lambda n: n["id"])
    for n in data["nodes"]:
        n.pop("content_hash", None)
        if isinstance(n.get("imports"), list):
            n["imports"] = sorted(n["imports"])
        if isinstance(n.get("imported_by"), list):
            n["imported_by"] = sorted(n["imported_by"])
    data["links"] = sorted(
        data["links"], key=lambda l: (l["source"], l["target"], l["kind"])
    )
    return data


# ── Test 1: golden / idempotency ─────────────────────────────────────────────

def test_golden_idempotent(tmp_path: Path) -> None:
    """Two consecutive analyze() runs on the same repo produce identical JSON."""
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_SRC, repo)
    _init_git(repo)

    run1 = _normalize(_run_analyze(repo))
    run2 = _normalize(_run_analyze(repo))

    assert run1["nodes"] == run2["nodes"], "Nodes differ between runs"
    assert run1["links"] == run2["links"], "Links differ between runs"
    assert run1["meta"]["total_files"] == run2["meta"]["total_files"]


# ── Test 2: DB populated after analyze ───────────────────────────────────────

def test_db_populated(tmp_path: Path) -> None:
    """After analyze(), the SQLite store contains the expected file rows."""
    from blastradius.store import Store
    from blastradius.index import db_path_for

    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_SRC, repo)
    _init_git(repo)

    data = _run_analyze(repo)

    db_path = db_path_for(repo)
    assert db_path.exists(), ".blastradius/index.db not created"

    store = Store(db_path)
    status = store.status()
    store.close()

    expected_files = len([n for n in data["nodes"]])
    assert status["active_files"] == expected_files, (
        f"DB has {status['active_files']} active files; "
        f"expected {expected_files}"
    )
    assert status["active_edges"] >= 0
    assert status["schema_version"] == "3"


# ── Test 3: incremental detection logs changed file ──────────────────────────

def test_incremental_detection(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """After editing a file and committing, the second analyze reports changed files."""
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_SRC, repo)
    _init_git(repo)

    # First full index
    _run_analyze(repo)
    capsys.readouterr()  # discard first-run output

    # Modify one file and commit it
    target = repo / "utils.py"
    original = target.read_text()
    target.write_text(original + "\n\n# modified\n")
    subprocess.run(["git", "add", "utils.py"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "modify utils"], cwd=repo,
                   check=True, capture_output=True)

    # Second incremental index
    _run_analyze(repo)
    captured = capsys.readouterr()

    # The incremental message must mention exactly 1 changed file
    assert "1 file(s) changed" in captured.err, (
        f"Expected incremental log message, got:\n{captured.err}"
    )


# ── Test 4: db status counts ─────────────────────────────────────────────────

def test_db_status_counts(tmp_path: Path) -> None:
    """store.status() counts match the number of nodes analyze() produced."""
    from blastradius.store import Store
    from blastradius.index import db_path_for

    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_SRC, repo)
    _init_git(repo)

    data = _run_analyze(repo)

    store = Store(db_path_for(repo))
    status = store.status()
    store.close()

    node_count = len(data["nodes"])
    link_count = len(data["links"])

    assert status["active_files"] == node_count
    assert status["active_edges"] == link_count
    assert status["last_indexed_commit"] != "none"


# ── Test 5: soft-delete on file removal ──────────────────────────────────────

def test_soft_delete_on_removal(tmp_path: Path) -> None:
    """Removing a file marks its DB row inactive, not deleted."""
    from blastradius.store import Store
    from blastradius.index import db_path_for

    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_SRC, repo)
    _init_git(repo)

    _run_analyze(repo)

    # Remove models.py and commit
    (repo / "models.py").unlink()
    subprocess.run(["git", "rm", "models.py"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove models"], cwd=repo,
                   check=True, capture_output=True)

    _run_analyze(repo)

    store = Store(db_path_for(repo))
    conn = store._conn
    active = conn.execute(
        "SELECT COUNT(*) FROM files WHERE active=1 AND path='models.py'"
    ).fetchone()[0]
    inactive = conn.execute(
        "SELECT COUNT(*) FROM files WHERE active=0 AND path='models.py'"
    ).fetchone()[0]
    store.close()

    assert active == 0, "Removed file should not be active"
    assert inactive == 1, "Removed file should be soft-deleted (inactive row kept)"

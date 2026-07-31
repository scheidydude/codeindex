# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
"""Phase 2 acceptance tests.

Acceptance criteria (from CKG-DESIGN-001):
  1. On a synthetic repo with a scripted history (add dep → remove dep),
     `impact FILE --as-of <old>` differs correctly from HEAD.
  2. `changed_since <ref>` lists the exact added/removed edges.
  3. Full backfill completes without modifying the working tree;
     commits table is populated.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "simple_python"


# ── helpers ──────────────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return r.stdout.strip()


def _setup_git(repo: Path) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _run_analyze(repo: Path) -> None:
    import importlib
    import blastradius.index as idx_mod
    importlib.reload(idx_mod)
    idx_mod.build(str(repo))


# ── Fixture: add-dep then remove-dep history ─────────────────────────────────

def _make_temporal_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """Create repo: C1=baseline, C2=add edge B→A, C3=remove edge.

    Returns (repo, c1_hash, c2_hash, c3_hash).
    Runs analyze() after each commit to populate temporal data.
    """
    repo = tmp_path / "temporal_repo"
    repo.mkdir()
    _setup_git(repo)

    # C1: two files, no inter-file import
    (repo / "a.py").write_text('"""Module A."""\n\nX = 1\n')
    (repo / "b.py").write_text('"""Module B."""\n\nY = 2\n')
    c1 = _commit_all(repo, "C1: initial")
    _run_analyze(repo)

    # C2: a.py now imports b.py
    (repo / "a.py").write_text('"""Module A."""\nfrom b import Y\n\nX = Y + 1\n')
    c2 = _commit_all(repo, "C2: add import b in a")
    _run_analyze(repo)

    # C3: a.py no longer imports b.py
    (repo / "a.py").write_text('"""Module A."""\n\nX = 42\n')
    c3 = _commit_all(repo, "C3: remove import")
    _run_analyze(repo)

    return repo, c1, c2, c3


# ── Test 1: as_of_impact differs between C2 and HEAD ─────────────────────────

def test_as_of_impact_differs_from_head(tmp_path: Path) -> None:
    """impact B --as-of C2 shows A as dependent; impact B at HEAD does not."""
    from blastradius.store import Store
    from blastradius.index import db_path_for, git_reachable, git_resolve

    repo, c1, c2, c3 = _make_temporal_repo(tmp_path)
    db_path = db_path_for(repo)
    store = Store(db_path)

    # At C2: a.py imports b.py → b.py has a direct dependent
    reachable_c2 = git_reachable(repo, c2)
    blast_at_c2 = store.as_of_impact("b.py", reachable_c2)
    assert blast_at_c2 is not None, "as_of_impact returned None for C2"
    assert blast_at_c2["direct_dependents"] >= 1, (
        f"Expected a.py as dependent of b.py at C2, got: {blast_at_c2}"
    )
    assert "a.py" in blast_at_c2["direct_ids"], (
        f"a.py not in direct_ids at C2: {blast_at_c2['direct_ids']}"
    )

    # At HEAD (C3): a.py no longer imports b.py → b.py has no dependents
    reachable_c3 = git_reachable(repo, c3)
    blast_at_head = store.as_of_impact("b.py", reachable_c3)
    assert blast_at_head is not None, "as_of_impact returned None for C3"
    assert blast_at_head["direct_dependents"] == 0, (
        f"Expected 0 dependents at C3, got: {blast_at_head}"
    )

    store.close()


# ── Test 2: changed_since lists added/removed edges ──────────────────────────

def test_changed_since_edges(tmp_path: Path) -> None:
    """changed_since(reachable_C1) shows added edge at C2 and its removal."""
    from blastradius.store import Store
    from blastradius.index import db_path_for, git_reachable

    repo, c1, c2, c3 = _make_temporal_repo(tmp_path)
    db_path = db_path_for(repo)
    store = Store(db_path)

    # Since C1: edge a→b was added (at C2) and removed (at C3, now inactive)
    reachable_c1 = git_reachable(repo, c1)
    result = store.changed_since(reachable_c1)
    store.close()

    # The removed edge (a→b, removed at C3) should appear in removed_edges
    removed_sources = {e["source"] for e in result["removed_edges"]}
    assert "a.py" in removed_sources, (
        f"Expected a.py in removed_edges sources. Got: {result['removed_edges']}"
    )


# ── Test 3: history backfill populates commits table ─────────────────────────

def test_history_backfill_commits(tmp_path: Path) -> None:
    """blastradius history populates the commits table."""
    from blastradius.store import Store
    from blastradius.index import db_path_for
    from blastradius.temporal import backfill

    repo = tmp_path / "hist_repo"
    shutil.copytree(FIXTURE_SRC, repo)
    _setup_git(repo)

    c1 = _commit_all(repo, "init")
    (repo / "models.py").write_text((repo / "models.py").read_text() + "\n# v2\n")
    c2 = _commit_all(repo, "modify models")

    # Analyze at HEAD only (don't analyze at c1)
    _run_analyze(repo)

    db_path = db_path_for(repo)
    store = Store(db_path)
    backfill(repo, store)
    store.close()

    store2 = Store(db_path)
    commit_count = store2._conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    store2.close()

    assert commit_count >= 2, (
        f"Expected ≥2 commits in table after backfill, got {commit_count}"
    )


# ── Test 4: history backfill sets first_seen_commit ──────────────────────────

def test_history_backfill_first_seen(tmp_path: Path) -> None:
    """After backfill, files have first_seen_commit set."""
    from blastradius.store import Store
    from blastradius.index import db_path_for
    from blastradius.temporal import backfill

    repo = tmp_path / "seen_repo"
    shutil.copytree(FIXTURE_SRC, repo)
    _setup_git(repo)
    _commit_all(repo, "init")

    _run_analyze(repo)

    db_path = db_path_for(repo)
    store = Store(db_path)
    backfill(repo, store)
    store.close()

    store2 = Store(db_path)
    null_count = store2._conn.execute(
        "SELECT COUNT(*) FROM files WHERE active=1 AND first_seen_commit IS NULL"
    ).fetchone()[0]
    store2.close()

    assert null_count == 0, (
        f"{null_count} active files still have NULL first_seen_commit after backfill"
    )


# ── Test 5: backfill never modifies working tree ─────────────────────────────

def test_history_no_working_tree_change(tmp_path: Path) -> None:
    """git status is clean before and after backfill (no checkout side-effects)."""
    from blastradius.store import Store
    from blastradius.index import db_path_for
    from blastradius.temporal import backfill

    repo = tmp_path / "clean_repo"
    shutil.copytree(FIXTURE_SRC, repo)
    _setup_git(repo)
    _commit_all(repo, "init")
    _run_analyze(repo)

    # Record file mtimes before backfill
    source_files = list(repo.rglob("*.py"))
    mtimes_before = {f: f.stat().st_mtime for f in source_files}

    db_path = db_path_for(repo)
    store = Store(db_path)
    backfill(repo, store)
    store.close()

    # Check no source file was modified
    for f in source_files:
        assert f.stat().st_mtime == mtimes_before[f], (
            f"Backfill modified working-tree file: {f}"
        )

    # Git status should show clean (only .blastradius/ and generated files differ)
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "*.py"],
        cwd=repo, capture_output=True, text=True,
    ).stdout.strip()
    assert status == "", f"Working tree dirty after backfill:\n{status}"


# ── Test 6: changed-since shows added file ────────────────────────────────────

def test_changed_since_added_file(tmp_path: Path) -> None:
    """changed_since(C1) shows a file added at C2."""
    from blastradius.store import Store
    from blastradius.index import db_path_for, git_reachable

    repo = tmp_path / "addfile_repo"
    shutil.copytree(FIXTURE_SRC, repo)
    _setup_git(repo)
    c1 = _commit_all(repo, "init")
    _run_analyze(repo)

    # Add a new file and commit
    (repo / "new_module.py").write_text('"""New module."""\nVALUE = 99\n')
    c2 = _commit_all(repo, "add new_module")
    _run_analyze(repo)

    db_path = db_path_for(repo)
    store = Store(db_path)
    reachable_c1 = git_reachable(repo, c1)
    result = store.changed_since(reachable_c1)
    store.close()

    assert "new_module.py" in result["added_files"], (
        f"new_module.py not in added_files. Got: {result['added_files']}"
    )

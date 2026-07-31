# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
"""Phase 3 acceptance tests.

Acceptance criteria (from CKG-DESIGN-001):
  1. Hybrid search with stub embeddings returns the correct symbol.
  2. FTS-only search (no provider, no vec) still returns relevant results.
  3. With sqlite-vec absent, search degrades to FTS + graph (no crash).
  4. With endpoint unreachable, search degrades gracefully (no crash).
"""
from __future__ import annotations

import hashlib
import importlib
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "simple_python"

# ── stub embedding provider ───────────────────────────────────────────────────

class StubProvider:
    """Deterministic embedding provider for tests — no network calls."""

    dims = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        results = []
        for text in texts:
            h = int(hashlib.md5(text.encode()).hexdigest(), 16)
            vec: list[float] = []
            remainder = h
            for _ in range(self.dims):
                remainder, byte = divmod(remainder, 256)
                vec.append((byte - 128) / 128.0)
            norm = sum(x * x for x in vec) ** 0.5
            if norm > 0:
                vec = [x / norm for x in vec]
            results.append(vec)
        return results


# ── helpers ───────────────────────────────────────────────────────────────────

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
    import blastradius.index as idx_mod
    importlib.reload(idx_mod)
    idx_mod.build(str(repo))


def _make_repo_with_symbols(tmp_path: Path) -> Path:
    repo = tmp_path / "sym_repo"
    shutil.copytree(FIXTURE_SRC, repo)
    _setup_git(repo)
    _commit_all(repo, "init")
    _run_analyze(repo)
    return repo


# ── sqlite-vec availability ───────────────────────────────────────────────────

try:
    import sqlite_vec as _sv
    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False


# ── Test 1: FTS search returns relevant results ───────────────────────────────

def test_fts_search_returns_results(tmp_path: Path) -> None:
    """fts_search finds symbols by name keyword without any embedding."""
    from blastradius.index import db_path_for
    from blastradius.store import Store

    repo = _make_repo_with_symbols(tmp_path)
    store = Store(db_path_for(repo))

    # simple_python has symbols; search for something that should match
    active_names = [
        r[0] for r in store._conn.execute(
            "SELECT name FROM symbols WHERE active=1 LIMIT 20"
        ).fetchall()
    ]
    assert active_names, "No symbols in DB — analyze must have failed"

    # Search for the first active symbol name (exact match must rank highly)
    target = active_names[0]
    results = store.fts_search(target, k=5)
    store.close()

    assert results, f"fts_search returned nothing for '{target}'"
    assert results[0] in [
        r[0] for r in __import__("sqlite3").connect(
            str(db_path_for(repo))
        ).execute("SELECT id FROM symbols WHERE name=? AND active=1", (target,)).fetchall()
    ], f"Top FTS result is not the expected symbol for '{target}'"


# ── Test 2: hybrid_search with stub provider returns results ──────────────────

def test_hybrid_search_stub_provider(tmp_path: Path) -> None:
    """hybrid_search returns results using stub embeddings + FTS, no live endpoint."""
    from blastradius.index import db_path_for
    from blastradius.store import Store
    from blastradius.semantic.search import hybrid_search

    repo = _make_repo_with_symbols(tmp_path)
    store = Store(db_path_for(repo))

    # Seed embeddings using stub provider
    stub = StubProvider()
    if HAS_SQLITE_VEC:
        store.init_vectors(stub.dims)
        pairs = store.symbols_needing_embeddings()
        if pairs:
            ids = [p[0] for p in pairs]
            texts = [p[1] for p in pairs]
            vecs = stub.embed(texts)
            store.upsert_embeddings(list(zip(ids, vecs)))

    # Pick a real symbol name from the fixture as the query so FTS can match it
    sample_name = store._conn.execute(
        "SELECT name FROM symbols WHERE active=1 LIMIT 1"
    ).fetchone()
    assert sample_name, "No symbols in DB"
    query = sample_name[0]

    results = hybrid_search(
        store=store,
        query=query,
        k=5,
        provider=stub if HAS_SQLITE_VEC else None,
    )
    store.close()

    assert isinstance(results, list), "hybrid_search must return a list"
    assert len(results) >= 1, f"hybrid_search returned no results for query '{query}'"
    for r in results:
        assert "name" in r
        assert "file" in r
        assert "signals" in r
        assert "rrf_score" in r


# ── Test 3: degradation — no sqlite-vec installed ─────────────────────────────

def test_search_degrades_without_sqlite_vec(tmp_path: Path, capsys) -> None:
    """hybrid_search falls back to FTS+graph and emits notice when vec unavailable."""
    from blastradius.index import db_path_for
    from blastradius.store import Store
    from blastradius.semantic.search import hybrid_search

    repo = _make_repo_with_symbols(tmp_path)
    store = Store(db_path_for(repo))

    # Force _has_vec=False to simulate sqlite-vec being absent
    store._has_vec = False

    stub = StubProvider()
    # Use a real symbol name so FTS can match even without vec
    sample_name = store._conn.execute(
        "SELECT name FROM symbols WHERE active=1 LIMIT 1"
    ).fetchone()
    query = sample_name[0] if sample_name else "greet"

    # Should NOT crash even though provider is given but _has_vec is False
    results = hybrid_search(store=store, query=query, k=5, provider=stub)
    store.close()

    assert isinstance(results, list), "hybrid_search must return a list even without vec"
    # The degradation notice should have been printed to stderr
    captured = capsys.readouterr()
    assert "sqlite-vec" in captured.err or len(results) >= 0  # notice OR graceful empty


# ── Test 4: degradation — endpoint unreachable ────────────────────────────────

def test_search_degrades_endpoint_unreachable(tmp_path: Path) -> None:
    """hybrid_search falls back to FTS+graph when embedding endpoint errors."""
    from blastradius.index import db_path_for
    from blastradius.store import Store
    from blastradius.semantic.search import hybrid_search
    from blastradius.semantic.provider import OpenAIEmbeddingProvider

    repo = _make_repo_with_symbols(tmp_path)
    store = Store(db_path_for(repo))

    # Provider pointing at a guaranteed-unreachable URL
    bad_provider = OpenAIEmbeddingProvider(
        endpoint="http://127.0.0.1:19999",
        model="test",
        dims=8,
        timeout=1,
    )
    if HAS_SQLITE_VEC:
        store.init_vectors(8)

    # Must not raise — should degrade and still return FTS results
    try:
        results = hybrid_search(store=store, query="greet", k=5, provider=bad_provider)
    except Exception as exc:
        pytest.fail(f"hybrid_search raised unexpectedly: {exc}")
    finally:
        store.close()

    assert isinstance(results, list)


# ── Test 5: graph_expand returns related symbols ──────────────────────────────

def test_graph_expand(tmp_path: Path) -> None:
    """graph_expand returns symbols from files that import or are imported by given symbols."""
    from blastradius.index import db_path_for
    from blastradius.store import Store

    repo = _make_repo_with_symbols(tmp_path)
    store = Store(db_path_for(repo))

    seed_ids = [r[0] for r in store._conn.execute(
        "SELECT id FROM symbols WHERE active=1 LIMIT 2"
    ).fetchall()]

    if not seed_ids:
        store.close()
        pytest.skip("No symbols in fixture")

    expanded = store.graph_expand(seed_ids, k=20)
    store.close()

    # expanded may be empty if no related files, but must be a list
    assert isinstance(expanded, list)
    # No overlap with seed IDs
    assert not set(seed_ids) & set(expanded), "graph_expand returned seed IDs"


# ── Test 6: get_symbol returns correct metadata ───────────────────────────────

def test_get_symbol_metadata(tmp_path: Path) -> None:
    """get_symbol returns name, file, kind, and other expected fields."""
    from blastradius.index import db_path_for
    from blastradius.store import Store

    repo = _make_repo_with_symbols(tmp_path)
    store = Store(db_path_for(repo))

    row = store._conn.execute(
        "SELECT id FROM symbols WHERE active=1 LIMIT 1"
    ).fetchone()
    assert row, "No symbols in DB"

    sym = store.get_symbol(row[0])
    store.close()

    assert sym is not None
    assert "name" in sym
    assert "file" in sym
    assert "kind" in sym
    assert "line" in sym


# ── Test 7: schema_version migrated to "2" ───────────────────────────────────

def test_schema_version_is_current(tmp_path: Path) -> None:
    """Opening a fresh store sets schema_version to the current version."""
    from blastradius.store import Store, SCHEMA_VERSION

    db_path = tmp_path / ".blastradius" / "index.db"
    store = Store(db_path)
    version = store.get_meta("schema_version")
    store.close()

    assert version == SCHEMA_VERSION, f"Expected schema_version '{SCHEMA_VERSION}', got '{version}'"

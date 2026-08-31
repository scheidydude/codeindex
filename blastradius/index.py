# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

"""Build and persist blastradius.json in the target repo root.

Phase-1 change: build() now also syncs graph data to a SQLite store at
<repo>/.blastradius/index.db.  The JSON write path is unchanged so existing
consumers keep working without modification.
"""

import hashlib
import json
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from blastradius.analyze import analyze
from blastradius.impact import compute_blast_radius, enrich_links, enrich_nodes
from blastradius.store import Store

INDEX_FILENAME = "blastradius.json"
_DB_DIR = ".blastradius"
_DB_NAME = "index.db"


def db_path_for(repo_root: Path) -> Path:
    return repo_root / _DB_DIR / _DB_NAME


def _git_head(root: Path) -> str | None:
    # Noninteractive Git children must never inherit MCP's live protocol pipe.
    # On Windows even inspecting that pipe during child startup can block.
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _git_changed(root: Path, from_commit: str, to_commit: str) -> set:
    """Return repo-relative paths changed between two commits."""
    try:
        r = subprocess.run(
            ["git", "diff", "--name-status", from_commit, to_commit],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        paths = set()
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                paths.add(parts[-1])  # rename lines have 3 parts; last is dest
        return paths
    except Exception:  # noqa: BLE001
        return set()


def git_modified(root: Path, from_ref: str, to_ref: str = "HEAD") -> list[str]:
    """Return repo-relative paths with content changes between two refs (status M only)."""
    try:
        r = subprocess.run(
            ["git", "diff", "--name-status", from_ref, to_ref],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        modified = []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].startswith("M"):
                modified.append(parts[-1])
        return modified
    except Exception:  # noqa: BLE001
        return []


def git_reachable(root: Path, ref: str) -> set:
    """Return the set of commit hashes reachable from ref (for as-of queries)."""
    try:
        r = subprocess.run(
            ["git", "log", "--format=%H", ref],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if r.returncode != 0:
            return set()
        return {line.strip() for line in r.stdout.splitlines() if line.strip()}
    except Exception:  # noqa: BLE001
        return set()


def git_resolve(root: Path, ref: str) -> str | None:
    """Resolve a ref (branch, tag, partial hash) to a full commit hash."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _content_hash(root: Path, rel_path: str) -> str | None:
    p = root / rel_path
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except (OSError, IsADirectoryError):
        return None


def _embed_new_symbols(store) -> None:
    """Embed symbols that lack a vector entry, if an embedding endpoint is configured.

    Reads BLASTRADIUS_EMBEDDING_ENDPOINT / _MODEL / _DIMS from env.
    Silently skips if any required env var is missing or if the call fails.
    """
    import os

    endpoint = os.environ.get("BLASTRADIUS_EMBEDDING_ENDPOINT", "")
    model = os.environ.get("BLASTRADIUS_EMBEDDING_MODEL", "")
    dims_str = os.environ.get("BLASTRADIUS_EMBEDDING_DIMS", "")
    if not (endpoint and model and dims_str):
        return
    try:
        dims = int(dims_str)
    except ValueError:
        return

    if not store.init_vectors(dims):
        return

    pairs = store.symbols_needing_embeddings()
    if not pairs:
        return

    try:
        from blastradius.semantic.provider import OpenAIEmbeddingProvider

        provider = OpenAIEmbeddingProvider(endpoint=endpoint, model=model, dims=dims)
        ids = [p[0] for p in pairs]
        texts = [p[1] for p in pairs]
        vecs = provider.embed(texts)
        store.upsert_embeddings(list(zip(ids, vecs)))
        store.set_meta("embedding_model", model)
        store._conn.commit()
        print(f"Embedded {len(ids)} symbol(s)", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: embedding failed: {exc}", file=sys.stderr)


def build(repo_path: str, output: Path | None = None) -> dict:
    root = Path(repo_path).resolve()
    data = analyze(str(root))

    blast = compute_blast_radius(data["nodes"], data["links"])
    enrich_nodes(data["nodes"], blast)
    enrich_links(data["nodes"], data["links"])

    data["meta"]["indexed"] = True

    # Attach content hashes before DB sync (used for future incremental detection)
    for node in data["nodes"]:
        node["content_hash"] = _content_hash(root, node["id"])

    # Detect changed file set for informational logging
    db_path = db_path_for(root)
    head_commit = _git_head(root)
    changed_paths: set | None = None

    with closing(Store(db_path)) as store:
        store.set_meta("repo_root", str(root))

        last_commit = store.get_meta("last_indexed_commit")
        if last_commit and head_commit and last_commit != head_commit:
            changed_paths = _git_changed(root, last_commit, head_commit)
            print(
                f"Incremental: {len(changed_paths)} file(s) changed since {last_commit[:8]}",
                file=sys.stderr,
            )
        elif last_commit is None:
            print("Incremental: first index — full scan", file=sys.stderr)

        store.sync(data, commit=head_commit, changed_paths=changed_paths)

        if head_commit:
            store.set_meta("last_indexed_commit", head_commit)
            store._conn.commit()

        # Build and sync symbol index
        try:
            from blastradius.symbols import build_symbol_index

            symbol_data = build_symbol_index(str(root))
            store.sync_symbols(symbol_data, commit=head_commit)
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: symbol sync failed: {exc}", file=sys.stderr)

        # Embed new symbols if an embedding endpoint is configured
        _embed_new_symbols(store)

    # Remove content_hash from in-memory data before JSON export to keep
    # the public schema unchanged.
    for node in data["nodes"]:
        node.pop("content_hash", None)

    dest = output or (root / INDEX_FILENAME)
    dest.write_text(json.dumps(data, indent=2))

    meta = data["meta"]
    langs_str = ", ".join(meta.get("languages", ["unknown"]))
    print(
        f"Indexed {meta['total_files']} files, {meta['total_loc']} LOC "
        f"[{langs_str}] → {dest}",
        file=sys.stderr,
    )
    return data


def load(index_path: Path) -> dict:
    if not index_path.exists():
        raise FileNotFoundError(
            f"{index_path} not found — run: blastradius analyze <repo>"
        )
    return json.loads(index_path.read_text())


def find_index(start: Path) -> Path | None:
    """Walk up from start looking for blastradius.json."""
    current = start.resolve()
    for _ in range(10):
        candidate = current / INDEX_FILENAME
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def find_db(start: Path) -> Path | None:
    """Walk up from start looking for .blastradius/index.db."""
    current = start.resolve()
    for _ in range(10):
        candidate = current / _DB_DIR / _DB_NAME
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None

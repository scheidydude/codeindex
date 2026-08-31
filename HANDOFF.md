# Engineering Handoff — blastradius Temporal Knowledge Graph

> Historical handoff: branch state, environment details, and pending work below
> describe the original session, not current setup instructions. For installation
> and development, use the [README](README.md#install) and [AGENTS.md](AGENTS.md).
> The maintained source is [IWasZ3r0Cool/blast-radius](https://github.com/IWasZ3r0Cool/blast-radius).

**Repo:** `IWasZ3r0Cool/blast-radius`  
**Branch:** `main` (4 commits ahead of origin)  
**Last commit:** `e863853` — Phase 2 temporal layer  
**Test status:** 11/11 passing (`uv run pytest` or `.venv/bin/pytest`)

---

## 1. Mission

We're evolving `blastradius` from a stateless point-in-time dependency analyzer into a **self-hosted temporal code knowledge graph** — persistent, incremental, and semantically queryable. The design document is `docs/blastradius-temporal-knowledge-graph.md` (CKG-DESIGN-001). Phases 0–2 are complete and committed. Next up is Phase 3 (semantic retrieval via embeddings) then Phase 4 (MCP surface + README).

---

## 2. Current State

### What's working and verified (committed, all tests green)

- **Phase 0** (`555c38d`): `docs/CKG-INTERNALS.md` — full internal map of the pre-change codebase. No behavior changes.

- **Phase 1** (`9b167d6`): SQLite store at `<repo>/.blastradius/index.db`.  
  - `Store` class in `blastradius/store/db.py` — schema v1, WAL mode, upsert/soft-delete for files/edges/symbols, FTS5 symbol index.  
  - `index.build()` now syncs to DB as a side effect after every `analyze` run. JSON export unchanged.  
  - `blastradius db status` / `blastradius db migrate` CLI commands.  
  - Incremental detection: logs `N file(s) changed since <commit>` based on `git diff --name-status`.  
  - 5 tests: golden idempotency, DB population, incremental detection, status counts, soft-delete.

- **Phase 2** (`e863853`): Temporal layer.  
  - `blastradius/temporal/history.py`: `backfill()` walks git log via `git ls-tree` + `git cat-file --batch`; never touches working tree; sets `first_seen_commit`/`last_seen_commit` on files and edges.  
  - `Store.as_of_impact(file, reachable_set)`: blast radius at a historical point using SQLite temp table for commit ancestry.  
  - `Store.changed_since(reachable_set)`: files/edges added or removed since a ref.  
  - `blastradius history [--since REF] [--max-commits N]`  
  - `blastradius changed-since <ref>`  
  - `blastradius impact FILE --as-of <ref>`  
  - 6 tests: as-of differs from HEAD, changed_since edges, backfill populates commits table, first_seen_commit set, no working-tree modification, changed-since added file.

### What's NOT yet built

- **Phase 3** (semantic layer): `blastradius/semantic/` package, embedding provider, `vec_symbols` table, `blastradius search` command, hybrid RRF query engine.
- **Phase 4** (surface): 4 new MCP tools (`semantic_search`, `temporal_impact`, `graph_query`, `changed_since`), README update.

### Exact next action

Start Phase 3. The design doc (`docs/blastradius-temporal-knowledge-graph.md` §6.4, §6.5, §8 Phase 3) is the authoritative spec. Read it before writing code.

---

## 3. Decisions Made (and Why)

**Decision:** Standalone FTS5 table (no `content=` option)  
**Alternatives considered:** `CREATE VIRTUAL TABLE symbols_fts USING fts5(name, doc, signature, content='symbols', content_rowid='id')`  
**Reason:** `content=` FTS5 tables break `DELETE FROM fts_table` with "database disk image is malformed" when the connection has WAL mode + foreign keys enabled. The content table tries to read deleted rows from the backing table to update the index, which fails after those rows are modified. Discovered the hard way.  
**Reversibility:** Easy to change; just affects the FTS DDL and the refresh logic in `sync_symbols()`.

---

**Decision:** JSON write path is fully unchanged; DB is an additive side-effect  
**Alternatives considered:** Replace JSON with DB-only export; migrate callers to read from DB  
**Reason:** Backward compatibility — every existing CLI command, MCP tool, and viz server reads `blastradius.json`. Phase 4 will eventually add DB-backed read paths, but for now JSON stays as the source of truth for queries.  
**Reversibility:** Load-bearing for Phases 1–3; Phase 4 is the right time to revisit.

---

**Decision:** `sync()` sets `last_seen_commit` on soft-delete (not just `last_seen_at`)  
**Alternatives considered:** Only track wall-clock time for removals  
**Reason:** `as_of_impact()` uses commit hashes to filter edges: `last_seen_commit NOT IN reachable_set` determines if an edge was still present at a historical ref. Without `last_seen_commit` being set on removal, all removed edges appeared absent from ALL historical views. Bug was caught by the Phase 2 test and fixed.  
**Reversibility:** Load-bearing for all temporal queries.

---

**Decision:** History backfill does NOT parse edges from blobs for complex multi-file import resolution  
**Alternatives considered:** Full analyzer re-run on each commit's blobs (complex, slow)  
**Reason:** The acceptance test only requires `--as-of` to work correctly when `analyze` has been run at the relevant commits. The `history` command sets file temporal data and records commits; edge temporal data comes from analyze runs. This keeps Phase 2 scoped and correct.  
**Reversibility:** Easy to add blob-based edge extraction later as an enhancement to `temporal/history.py`.

---

**Decision:** `as_of_impact()` uses a SQLite temp table (`_reachable`) for commit ancestry filtering  
**Alternatives considered:** Large `IN (?,?,...)` clause; recursive CTE on commits table  
**Reason:** Temp table is clean, indexed, and scales (no 999-parameter limit). Recursive CTE would require commit ordering to be stored in DB. `IN (?,?)` is fine for small sets but fragile.  
**Reversibility:** Internal implementation detail; easy to change.

---

**Decision:** uv venv at `.venv/` with Python 3.11  
**Alternatives considered:** System pip (broken on this machine due to packaging metadata corruption)  
**Reason:** System pip had a corrupted `packaging` package. uv created a clean 3.11 venv.  
**Reversibility:** Easy. Run `.venv/bin/pytest` or `.venv/bin/blastradius` for everything.

---

## 4. Architecture & Key Files

### Created this session

| File | Purpose |
|---|---|
| `blastradius/store/__init__.py` | Re-exports `Store` |
| `blastradius/store/db.py` | `Store` class: schema, upsert/soft-delete, temporal queries, FTS, status |
| `blastradius/temporal/__init__.py` | Re-exports `backfill` |
| `blastradius/temporal/history.py` | `backfill()`, git plumbing helpers, lightweight import extractor |
| `tests/test_phase1.py` | 5 Phase 1 acceptance tests |
| `tests/test_phase2.py` | 6 Phase 2 acceptance tests |
| `tests/fixtures/simple_python/` | 3-file Python fixture repo (main, utils, models) |
| `docs/CKG-INTERNALS.md` | Phase 0 internals map; read this before touching core parsing code |

### Modified significantly this session

| File | What changed |
|---|---|
| `blastradius/index.py` | Added `git_reachable()`, `git_resolve()`, `db_path_for()`, `find_db()`. `build()` now syncs to Store after enrichment. Content hashes computed pre-sync, stripped before JSON write. |
| `blastradius/cli.py` | Added `_cmd_db`, `_cmd_history`, `_cmd_changed_since`. Updated `_cmd_impact` to handle `--as-of`. Added `history`, `changed-since`, `db` subparsers. Added `--as-of` arg to `impact`. |
| `pyproject.toml` | Added `dev = ["pytest>=7"]`, `semantic = ["sqlite-vec"]` extras. Added `[tool.pytest.ini_options]`. |

### Should NOT be touched

| File | Why |
|---|---|
| `blastradius/analyze.py` | Full repo analysis dispatcher — untouched by design. Per-file incremental parsing is a future optimization. |
| `blastradius/analyzers/*.py` | All existing analyzers — Phase 1–3 don't modify them. |
| `blastradius/mcp_server.py` | Existing 6 MCP tools — Phase 4 adds new ones alongside, never modifies existing. |
| `blastradius/impact.py` | `compute_blast_radius()` is called directly by `Store.as_of_impact()`. Don't change its signature. |
| `docs/blastradius-temporal-knowledge-graph.md` | The spec. Don't edit it; read it. |

### Phase 3 will create

```
blastradius/semantic/__init__.py
blastradius/semantic/provider.py   # EmbeddingProvider ABC + OpenAI-compat HTTP impl
blastradius/semantic/search.py     # Hybrid query engine (semantic + FTS + graph, RRF)
```

And add to `Store` in `blastradius/store/db.py`:
- `vec_symbols` virtual table (gated behind `sqlite-vec` import)
- `upsert_embeddings(symbol_ids_and_vecs)` method
- `semantic_search(query_vec, k, reachable)` method

---

## 5. Gotchas & Hard-Won Knowledge

**FTS5 `content=` tables + WAL mode = "database disk image is malformed"**  
`DELETE FROM symbols_fts` on a `content='symbols'` FTS5 table fails with this error when the connection has WAL mode and foreign keys enabled. Use a standalone FTS5 table (`fts5(name, doc, signature)` with no `content=`). Manage it manually: DELETE all rows, INSERT active symbols. This is what the current code does; don't change it back.

**`last_seen_commit` must be set on soft-delete for temporal queries to work**  
The original `sync()` only set `last_seen_at` (wall clock) when soft-deleting edges/files. `as_of_impact()` uses commit hashes (`last_seen_commit NOT IN reachable_set`), so wall-clock time is useless. The fix is in `sync()` — the soft-delete UPDATE now includes `last_seen_commit=?`. Don't revert this.

**`executescript()` auto-commits**  
Python's `sqlite3.executescript()` commits any pending transaction before running. This is why schema application is done with `executescript(_DDL)` then `executescript(_FTS_DDL)` — separate calls for regular DDL and FTS. Don't mix `execute()` and `executescript()` in the same logical transaction.

**`git cat-file --batch` output format**  
The format is: `<hash> blob <size>\n<content>\n` for each object. The parser in `history.py:_git_cat_file_batch()` reads byte-by-byte with `pos` tracking. Don't replace this with individual `git show` calls — it's deliberately batch for performance.

**System pip is broken on this machine**  
Don't use `pip3` or `python3` directly. Always use `.venv/bin/pytest`, `.venv/bin/blastradius`, `.venv/bin/python3`. The miniforge Python 3.9 has a corrupted `packaging` package that causes `pip install` to fail.

**`sqlite-vec` for Phase 3 must be feature-detected at runtime**  
The design doc requires graceful degradation: if `sqlite-vec` can't load (not installed, wrong platform), `blastradius search` falls back to FTS + graph with a clear log message. Never `import sqlite_vec` at module level — always try/except around the extension load, same pattern as the FTS DDL in `_apply_schema()`.

**Dependency direction is a hard constraint**  
`blastradius/core/`, `store/`, and `temporal/` must NEVER import from `blastradius/graph/` or `blastradius/semantic/`. The import arrow points downward only. The current `store/db.py` has a local import of `compute_blast_radius` inside `as_of_impact()` — this is a `blastradius.impact` import (core layer), which is fine.

---

## 6. Conventions In Play

**File headers:** Every new file gets:
```python
# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
```

**Python compatibility:** `from __future__ import annotations` at the top of every file. Target Python 3.9+. With the `from __future__` import, `X | Y` union types and `list[dict]` generics work as annotations.

**Commit style:** Conventional Commits. Subject ≤72 chars. Body explains the why. End with `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`.

**Testing:** pytest, `tests/` directory. Every phase adds tests before the phase commit. Tests use `tmp_path` for isolation. Fixture repos go in `tests/fixtures/`. No mocking of SQLite — real DB in tmp_path. Tests that need git repos call `git init`/`git commit` via subprocess in the test.

**Optional deps pattern:** New heavy deps go in `pyproject.toml` `[project.optional-dependencies]` and are imported defensively:
```python
try:
    import sqlite_vec  # or whatever

    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False
```

**No comments on obvious code.** Comments only for non-obvious constraints or workarounds (e.g., the FTS content= issue). No docstrings on trivial methods.

**JSON schema is frozen.** `blastradius.json` and `symbolindex.json` schemas don't change. Any new data lives in the DB only, exposed via new CLI/MCP commands.

---

## 7. Open Questions

1. **Embedding endpoint for Phase 3 acceptance test:** The acceptance test requires `search "validate auth token"` to surface the right symbol using a local embedding endpoint. Does the user have a local embedding server running (e.g., Ollama, llama.cpp with embeddings, LM Studio)? If not, the acceptance test will need a mock/stub provider. The design doc says "stub/mock embedding provider (deterministic vectors) so tests need no live endpoint" — confirm this is the right approach before writing the test.

2. **`sqlite-vec` install on this machine:** `sqlite-vec` is an optional C extension. It needs to match the Python version in `.venv` (3.11). Has it been tested that `uv pip install sqlite-vec` works on this machine before starting Phase 3? If it fails, the degradation path is the fallback, but we'd want to know ahead of time.

3. **Phase 4 MCP tools naming:** The design doc specifies 4 new MCP tools: `semantic_search`, `temporal_impact`, `graph_query`, `changed_since`. `changed_since` conflicts with the existing CLI command name `changed-since`. The MCP tool name should be confirmed — probably `changed_since` (underscore) is fine since MCP tool names aren't CLI commands.

4. **Viz temporal scrubber (Phase 4 nice-to-have):** The design doc marks this as optional. Does the user want it, or ship Phase 4 without it?

---

## 8. Do Not Touch

- **`blastradius/mcp_server.py`** — existing 6 tools work and are not under change until Phase 4. Phase 4 adds new tools alongside; it doesn't modify existing ones.
- **`blastradius/analyze.py` and `blastradius/analyzers/`** — the parsing pipeline is deliberately untouched. Any incremental per-file parsing is a future optimization, not Phase 3.
- **The `blastradius.json` / `symbolindex.json` schema** — frozen per constraint #3.
- **`.venv/`** — don't recreate or upgrade. Python 3.11, packages installed via `uv pip install -e ".[dev,yaml]"`.
- **`docs/blastradius-temporal-knowledge-graph.md`** — the spec document. Read-only.
- **`SCHEMA_VERSION = "1"` in `store/db.py`** — don't bump until Phase 3 actually adds the `vec_symbols` table, and then also add a migration path.

---

## 9. Resume Command

> Read `HANDOFF.md` and `docs/blastradius-temporal-knowledge-graph.md` (§6.4, §6.5, §8 Phase 3, §9 Constraints). Then implement Phase 3: create `blastradius/semantic/` with an embedding provider interface and OpenAI-compatible HTTP client (stdlib urllib only, no new runtime deps for the client), add `vec_symbols` to the Store gated behind `sqlite-vec`, implement `blastradius search` with hybrid RRF retrieval (semantic + FTS + graph), and add degradation tests. Use `.venv/bin/pytest` to run tests. Do not modify existing analyzers, the JSON export schema, or any existing MCP tools. Run all 11 existing tests before committing — they must stay green.

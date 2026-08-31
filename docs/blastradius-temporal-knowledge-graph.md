# blastradius → Temporal Code Knowledge Graph

| | |
|---|---|
| **Doc ID** | CKG-DESIGN-001 |
| **Status** | Draft — ready for implementation |
| **Target repo** | `IWasZ3r0Cool/blast-radius` |
| **Audience** | Claude Code (implementing agent) + maintainer |
| **License constraint** | Apache 2.0 (preserve existing headers) |

> **How to use this document with Claude Code**
> Work it phase by phase. Do **not** attempt the whole thing in one pass. Complete a phase, run its acceptance checks, commit, then move on. Phase 0 is mandatory before writing any code — it grounds you in the existing structure so later phases integrate cleanly instead of duplicating logic. Treat the "Constraints" section as hard rules, not suggestions.

---

## 1. Problem statement

`blastradius` today is a **stateless, point-in-time, exact-match** dependency and symbol analyzer. It parses a repo, computes a dependency graph with blast-radius scores plus a symbol map, and writes `blastradius.json` / `symbolindex.json`. That design is excellent for a one-shot snapshot but blocks three capabilities needed to serve as a durable knowledge graph for AI agents:

1. **Stateless.** Every run recomputes the entire index from scratch. There is no persistent store, no incremental update, and no cheap repeated querying. This does not scale to an always-on agent asking many questions, or to large repos.
2. **Atemporal.** The graph only ever represents *now*. It cannot answer how structure evolved — when a dependency was introduced, what the blast radius of a file was at a past release, or which files churn most — which is core to real impact analysis and code archaeology.
3. **Exact-match retrieval only.** Symbols are found by exact name (`symbols["verify_token"]`). There is no semantic retrieval — an agent cannot ask "find the code that validates auth tokens" without already knowing the name.

## 2. Goal

Evolve `blastradius` into a **self-hosted, local-first temporal code knowledge graph with hybrid retrieval** — conceptually "Graphiti applied to code instead of conversations" — while preserving everything that makes the current tool good: deterministic AST extraction, near-zero-friction install, and the existing CLI / JSON / MCP / viz surfaces.

The end state adds three properties to the existing graph: **persistence + incrementality**, **time**, and **meaning (semantic retrieval)**.

## 3. Non-goals (explicit scope boundaries)

These are out of scope. Do not implement them, and do not refactor toward them.

- **Not conversational / agent memory.** This project does not store user preferences, chat history, or "who the user is." That is a separate concern that belongs in a downstream memory layer. `blastradius` is a *code-context provider*, not a personal-memory store. Keep the two truth models separate (deterministic code facts vs. probabilistic conversational facts).
- **Not LLM-based extraction.** Do not replace deterministic AST/regex extraction with an LLM. The deterministic model is a reliability advantage and must remain the source of graph structure. LLMs/embeddings are used *only* for the semantic retrieval layer, never to infer edges or symbols.
- **Not a server-based graph database.** No Neo4j, FalkorDB, or any external graph service. The store stays embedded.
- **No breaking changes to the public contract.** The `blastradius.json` / `symbolindex.json` schemas and existing CLI commands must keep working unchanged.
- **No cloud dependency.** Embeddings must be obtainable from a local/self-hosted endpoint. Nothing leaves the machine by default.

## 4. Architecture overview

```
Source: repos + git history
        │
        ▼
Language parsers              (EXISTING — deterministic AST)
        │
        ▼
Graph store (SQLite)          (NEW — nodes · edges · symbols; replaces JSON as source of truth)
        │
   ┌────┴─────┐
   ▼          ▼
Temporal     Semantic         (NEW — commit-versioned edges; embeddings via sqlite-vec)
   │          │
   └────┬─────┘
        ▼
Hybrid query engine           (NEW — exact · semantic · graph · temporal fusion)
        │
        ▼
Surfaces                      (EXISTING, extended — MCP tools · CLI · viz)
```

The migration is deliberately layered so each layer is shippable on its own:

- **Phase 1** gives you persistence + incrementality with *no new dependencies*.
- **Phase 2** adds time, still with no embedding/model dependency.
- **Phase 3** adds semantic retrieval — the only phase that introduces an optional dependency and an external model.
- **Phase 4** exposes everything through the query surfaces.

### 4.1 Package layout & extraction boundary

This work ships as a **feature release on the existing `blastradius` repo**, *not* a fork and *not* (yet) a separate project. But it is laid out so the heavy layer can be extracted into its own package later with a clean lift, if it grows an independent identity. To keep that door open, the codebase is partitioned into a **lean core** and **optional layers**, and the dependency direction is strictly one-way.

```
blastradius/
  core/        # deterministic parsers, blast scoring — the lean primitive
  store/       # SQLite store + incremental indexing      (core dependency-free)
  temporal/    # commit stamping + git-history ingestion   (core dependency-free)
  graph/       # hybrid query engine, agent-facing graph    (extractable layer)
  semantic/    # embedding provider + sqlite-vec            (extractable layer; optional dep)
```

**Hard partitioning rules:**

- `core/`, `store/`, and `temporal/` **must not import** from `graph/` or `semantic/`. The dependency arrow points only *up*, from the heavy layers down into the core — never the reverse. This is what makes a future extraction a move, not a rewrite.
- This design proposes optional graph and semantic layers. Currently, `blastradius-cli[semantic]` is the supported semantic extra; there is no separate `graph` extra. The default installation now includes the official MCP SDK and its dependencies and requires Python 3.10+. The core layering remains unchanged. Current installation and supported extras are documented in the [README](../README.md#install).
- The heavy layers consume the core through its public store/graph API only — never by reaching into core internals — so that when `graph/`+`semantic/` are lifted into a standalone package, the only change is declaring `blastradius` as a dependency.

## 5. Data model

The canonical store becomes a single SQLite database (default: `<repo>/.blastradius/index.db`). `blastradius.json` / `symbolindex.json` become **exports** (a view rendered from the DB), preserving the current public schema exactly.

### 5.1 Temporal model

Bi-temporal, kept simple:

- **Commit time** — where a fact lives in git history. Every node/edge/symbol carries `first_seen_commit` and `last_seen_commit` (nullable; `NULL` last-seen = still present at HEAD).
- **Index time** — when `blastradius` observed the fact (`first_seen_at`, `last_seen_at`, ISO-8601).

Facts are **never hard-deleted**. When a file/edge/symbol disappears, set its `last_seen_commit` and `last_seen_at` and flag it inactive. This is what enables "as-of" queries and churn analysis.

### 5.2 Schema (authoritative starting point — refine during Phase 1)

```sql
-- Schema/version metadata and indexing state
CREATE TABLE index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- expected keys: schema_version, last_indexed_commit, repo_root,
--                embedding_model, embedding_dims, created_at

CREATE TABLE commits (
    hash         TEXT PRIMARY KEY,
    authored_at  TEXT,
    committed_at TEXT,
    author       TEXT,
    message      TEXT,
    parent_hash  TEXT
);

CREATE TABLE files (
    id                INTEGER PRIMARY KEY,
    path              TEXT NOT NULL,            -- repo-relative
    language          TEXT,
    layer             TEXT,                     -- backend/frontend/etc.
    loc               INTEGER,
    content_hash      TEXT,                     -- for incremental change detection
    blast_score       REAL,
    direct_dependents INTEGER DEFAULT 0,
    transitive_dependents INTEGER DEFAULT 0,
    active            INTEGER NOT NULL DEFAULT 1,
    first_seen_commit TEXT,
    last_seen_commit  TEXT,
    first_seen_at     TEXT,
    last_seen_at      TEXT,
    UNIQUE (path)
);

CREATE TABLE edges (
    id                INTEGER PRIMARY KEY,
    source_file_id    INTEGER NOT NULL REFERENCES files(id),
    target_file_id    INTEGER NOT NULL REFERENCES files(id),
    kind              TEXT NOT NULL,            -- imports/depends_on/foreign_key/needs/...
    weight            REAL DEFAULT 1,
    active            INTEGER NOT NULL DEFAULT 1,
    first_seen_commit TEXT,
    last_seen_commit  TEXT,
    first_seen_at     TEXT,
    last_seen_at      TEXT,
    UNIQUE (source_file_id, target_file_id, kind)
);

CREATE TABLE symbols (
    id                INTEGER PRIMARY KEY,
    file_id           INTEGER NOT NULL REFERENCES files(id),
    name              TEXT NOT NULL,
    kind              TEXT,                     -- function/class/struct/enum/trait/...
    line              INTEGER,
    exported          INTEGER DEFAULT 0,
    signature         TEXT,
    doc               TEXT,
    active            INTEGER NOT NULL DEFAULT 1,
    first_seen_commit TEXT,
    last_seen_commit  TEXT,
    first_seen_at     TEXT,
    last_seen_at      TEXT
);

CREATE INDEX idx_edges_source ON edges(source_file_id);
CREATE INDEX idx_edges_target ON edges(target_file_id);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_file ON symbols(file_id);
CREATE INDEX idx_files_active  ON files(active);

-- Full-text retrieval (stdlib SQLite, no extra dependency)
CREATE VIRTUAL TABLE symbols_fts USING fts5(
    name, doc, signature,
    content='symbols', content_rowid='id'
);

-- Vector retrieval (Phase 3 only; requires sqlite-vec extension)
-- CREATE VIRTUAL TABLE vec_symbols USING vec0(
--     symbol_id INTEGER PRIMARY KEY,
--     embedding FLOAT[768]
-- );
```

Bump `schema_version` and ship a forward migration whenever this changes.

## 6. Component design

### 6.1 Storage layer (`blastradius/store/`)
A thin module wrapping the SQLite connection: schema creation, versioned migrations, upsert/close helpers for files/edges/symbols, and JSON export that reproduces the current `blastradius.json` / `symbolindex.json` byte-for-byte (golden-tested). The store is the new source of truth; everything else reads/writes through it.

### 6.2 Incremental indexer
Replaces full recompute. Algorithm:

1. Resolve current state: `git rev-parse HEAD` if in a git repo; otherwise fall back to working-tree scan.
2. Determine the changed set:
   - Git mode: `git diff --name-status <last_indexed_commit> HEAD` plus any dirty working-tree files.
   - No-git / dirty mode: compare `content_hash` of each on-disk file against the stored hash.
3. Re-parse **only** added/modified files using the existing parsers. Mark deleted files inactive (set `last_seen_*`).
4. Diff the freshly parsed nodes/edges/symbols against the stored set; upsert new/changed, close removed.
5. **Recompute blast scores only for the affected subgraph** — the transitive dependents of any file whose edges changed — not the whole repo. (Collect touched nodes, walk `imported_by` transitively, recompute `direct/transitive_dependents` and `blast_score` for that set.)
6. Refresh FTS rows for changed symbols. (Embeddings handled in Phase 3.)
7. Update `index_meta.last_indexed_commit` and export JSON.

### 6.3 Temporal ingestion (`blastradius history`)
Backfills the temporal graph from git history **without checkouts**, to keep it safe and fast:

- Walk `git log` oldest→newest within bounds (`--since`, `--max-commits`).
- For each commit, list tree state with `git ls-tree -r <commit>` and read blob contents via `git cat-file --batch` (stream; never touch the working tree).
- Parse changed blobs per commit (diff against previous tree), updating `first_seen_commit` / `last_seen_commit` on nodes/edges/symbols accordingly.
- Record each commit in `commits`.

This is the most performance-sensitive component — see Risks. Default to **bounded** backfill; full-history is opt-in.

### 6.4 Semantic layer (Phase 3)
- **What gets embedded:** per symbol, the concatenation of `name + signature + doc` (skip empties).
- **Embedding provider interface** (`blastradius/semantic/`): a small abstraction with one shipped implementation — an **OpenAI-compatible `/v1/embeddings` HTTP client** built on stdlib `urllib` (no new dependency for the client itself). Endpoint URL, model name, and dimensions come from config/env. This targets a self-hosted local inference server out of the box.
- **Vector store:** `sqlite-vec` `vec0` virtual table. This is the single new optional dependency, gated behind the `blastradius-cli[semantic]` extra. If the extension can't load, semantic features degrade gracefully (log a clear message, fall back to FTS + exact) — they never hard-fail the tool.
- Embeddings are (re)generated only for new/changed symbols during indexing, batched.

### 6.5 Hybrid query engine
`query(text, k, as_of=None)` fuses three retrieval signals and optionally filters by time:

1. **Semantic** — KNN over `vec_symbols` (if available).
2. **Keyword** — FTS5 over `symbols_fts`.
3. **Graph expansion** — for top candidates, pull dependents/dependencies to add structurally related results.
4. **Temporal filter** — if `as_of` (a commit/ref) is given, restrict to facts whose `[first_seen_commit, last_seen_commit]` window contains that commit.

Combine the ranked lists with **Reciprocal Rank Fusion** (`score = Σ 1/(60 + rank_i)`) — no weights to tune. Return symbols with file, line, blast score, and provenance (which signals matched).

## 7. Public interfaces

### 7.1 CLI (additions; existing commands unchanged)
| Command | Purpose |
|---|---|
| `blastradius analyze` | Existing — now incremental, writes to SQLite, still exports `blastradius.json` |
| `blastradius history [--since REF] [--max-commits N]` | Backfill the temporal graph from git history |
| `blastradius search "<query>" [--k N] [--as-of REF] [--json]` | Hybrid semantic + keyword + graph search |
| `blastradius impact FILE [--as-of REF]` | Existing — gains optional point-in-time blast radius |
| `blastradius db status` | Show schema version, last indexed commit, counts, embedding config |
| `blastradius db migrate` | Apply pending schema migrations |

### 7.2 MCP tools (additions; existing four unchanged)
| Tool | Description |
|---|---|
| `semantic_search` | `query`, `k`, optional `as_of` → ranked symbols with provenance |
| `temporal_impact` | `file`, optional `as_of` → blast radius at a point in time |
| `graph_query` | `file`, `direction` (dependents/dependencies), `depth` → k-hop neighborhood |
| `changed_since` | `ref` → files/edges/symbols added or removed since a ref |

### 7.3 Configuration
Read from `[tool.blastradius]` in `pyproject.toml` and/or `.blastradius.toml`, env vars override. Keys: `db_path`, `embedding_endpoint`, `embedding_model`, `embedding_dims`, `history_max_commits`. All have safe defaults; semantic config is optional.

## 8. Implementation phases

> Each phase ends with: all existing tests green, new tests added, acceptance checks passing, a commit. Keep the core dependency-free; every new dependency is an optional extra.

### Phase 0 — Orient (no behavior change)
- Read the existing package layout under `blastradius/` and `viz/`. Produce `docs/CKG-INTERNALS.md`: current module map, where parsing happens, where JSON is written, how the MCP server and CLI dispatch are wired.
- Identify the integration seams for the new store without changing behavior.
- **Acceptance:** internals doc committed; no source changes; existing tests still pass.

### Phase 1 — SQLite store + incremental indexing (no new deps)
- Implement `blastradius/store/` (schema, migrations, upsert/close, JSON export).
- Wire `analyze` to populate the DB and export `blastradius.json` / `symbolindex.json` from it.
- Implement incremental change detection (git diff + content-hash fallback) and affected-subgraph blast recompute.
- Populate `symbols_fts`.
- **Acceptance:**
  - Exported JSON is byte-identical to pre-change output on a fixture repo (golden test).
  - Re-running `analyze` after editing one file re-parses only that file's change set (assert via instrumentation/log).
  - `blastradius db status` reports correct counts.

### Phase 2 — Temporal layer (no new deps)
- Implement `blastradius history` via `git ls-tree` + `git cat-file --batch` (no checkouts), bounded by `--since` / `--max-commits`.
- Maintain `first_seen_commit` / `last_seen_commit` on nodes/edges/symbols; populate `commits`.
- Add `--as-of` to `impact`; implement `changed_since`.
- **Acceptance:**
  - On a synthetic repo with a scripted history (add dep → remove dep), `impact FILE --as-of <old>` differs correctly from HEAD.
  - `changed_since <ref>` lists the exact added/removed edges.
  - Full backfill of a mid-size repo completes within a documented bound; never modifies the working tree.

### Phase 3 — Semantic layer (optional dep: `sqlite-vec`)
- Implement the embedding-provider interface + stdlib OpenAI-compatible client.
- Add `vec_symbols`; embed `name+signature+doc` for new/changed symbols, batched.
- Implement the hybrid query engine (semantic + FTS + graph, RRF fusion, optional temporal filter) behind `blastradius search`.
- Graceful degradation when the extension or endpoint is unavailable.
- **Acceptance:**
  - With a local endpoint configured, `search "validate auth token"` surfaces the auth-validation symbol without its name in the query.
  - With `sqlite-vec` absent, `search` still returns keyword+graph results and prints a clear notice — no crash.

### Phase 4 — Surface integration
- Register the four new MCP tools alongside the existing ones.
- Optional: add a temporal control to the viz UI (scrub blast radius across commits) — nice-to-have, not blocking.
- Update `README.md` with the new commands, the DB store, and the `[semantic]` extra.
- **Acceptance:** new MCP tools callable from an MCP client; README documents every new command and config key.

## 9. Constraints for the implementing agent (hard rules)

Historical constraints for the original temporal work follow. The later MCP SDK
migration supersedes the package-wide dependency and Python-version promises in
items 1 and 9: the default package now includes MCP dependencies and requires
Python 3.10+. The core layering and optional semantic-extension constraints remain.

1. **Preserve the zero-dependency core.** Phases 0–2 add **no** runtime dependencies. `sqlite-vec` (Phase 3) is the only new dependency and must be an optional extra (`blastradius-cli[semantic]`); the tool must run fully without it.
2. **Respect the partitioning and dependency direction (§4.1).** `core/`, `store/`, and `temporal/` must never import from `graph/` or `semantic/`. The heavy layers depend on the core, never the reverse, and consume it only through its public store/graph API. This keeps a future extraction into a standalone package a clean lift. Any change that creates a core→heavy import is a hard failure.
3. **Do not change the public JSON schema or existing CLI commands.** JSON output stays byte-compatible (golden-tested).
4. **Deterministic extraction is sacred.** No LLM or heuristic inference of edges/symbols. Embeddings serve retrieval only.
5. **No external services.** No graph DB, no cloud APIs. Embeddings come from a configurable local endpoint; absence degrades gracefully.
6. **Never modify the working tree during history ingestion.** Read blobs via git plumbing only.
7. **Every phase keeps existing tests green and adds tests for new code.** No phase regresses the prior phase.
8. **Apache 2.0 headers and `Copyright 2026 David Scheiderman` preserved** on new files.
9. **Python 3.9+ compatibility** maintained.

## 10. Testing strategy
- `pytest`, with small fixture repos checked into `tests/fixtures/` covering multiple languages.
- **Golden tests** for JSON export parity (Phase 1 regression guard).
- **Temporal tests** built on a synthetic git history created in a tmp dir within the test (script commits, assert as-of results).
- **Retrieval smoke tests** for hybrid search using a stubbed/mock embedding provider (deterministic vectors) so tests need no live endpoint.
- **Degradation tests:** semantic path with `sqlite-vec` unavailable and with endpoint unreachable.

## 11. Risks & mitigations
| Risk | Mitigation |
|---|---|
| Git history ingestion is slow on large repos | Read blobs via `cat-file --batch` (no checkouts); bound backfill by default (`--max-commits`); make full history opt-in; batch DB writes in transactions |
| Embedding endpoint unavailable | Semantic layer is optional and degrades to keyword+graph; clear logging; never hard-fail |
| `sqlite-vec` extension load fails on a platform | Feature-detect at startup; fall back; document install per OS |
| Schema evolution breaks existing DBs | Versioned migrations + `schema_version` in `index_meta`; `db migrate` command |
| Incremental diff misses a change | Content-hash fallback validates against git diff; provide `analyze --full` escape hatch to force a rebuild |
| Scope creep into conversational memory | Section 3 forbids it; `blastradius` stays a code-context provider only |

## 12. Definition of done
The project is complete when: `analyze` is incremental and SQLite-backed with byte-identical JSON export; `history` builds a queryable temporal graph; `impact --as-of` and `changed_since` work; `search` performs hybrid retrieval with graceful degradation; all four new MCP tools are registered; the README documents everything; and the full test suite (golden + temporal + retrieval + degradation) passes with the core remaining dependency-free.

# CKG-INTERNALS: blastradius Internal Architecture

| | |
|---|---|
| **Doc ID** | CKG-INTERNALS-001 |
| **Status** | Phase 0 — orient |
| **Audience** | Implementing agent (Phase 1+) |
| **Generated** | 2026-06-07 |

Produced per Phase 0 of CKG-DESIGN-001. Documents current module structure, data flow, and integration seams for the SQLite store without changing any behavior.

---

## 1. Module Map

```
blastradius/
  __init__.py              — package metadata; version "0.1.0"
  cli.py                   — argparse CLI; 8 commands; dispatch table at line 353
  index.py                 — build() orchestrator; load() reader; find_index() discovery
  analyze.py               — language detection; analyzer dispatch; result merging
  impact.py                — BFS blast radius; enrich_nodes(); enrich_links()
  symbols.py               — symbol index builder; write_standalone(); write_inline()
  symbol_extractor.py      — per-language symbol extraction (AST + regex)
  mcp_server.py            — SDK-backed stdio MCP server; 10 tools
  artifacts.py             — per-request artifact resolution and database lifetime
  viz_server.py            — HTTP server for D3/Three.js viz; /graph + /refresh endpoints
  hook.py                  — git pre-commit hook installer
  reporter.py              — blast report formatter (stdout / markdown / JSON)
  analyzers/
    base.py                — shared: gitignore parsing, skip-dir logic, file grouping
    python_analyzer.py     — AST-based imports; internal module resolution
    js_analyzer.py         — regex; components, hooks, routes, stores
    go_analyzer.py         — package-level nodes; import resolution
    ruby_analyzer.py       — require-based; Rails detection
    java_analyzer.py       — two-pass FQN mapping + import resolution
    rust_analyzer.py       — pub fn / struct / enum / trait via regex
    php_analyzer.py        — class / interface / function via regex
    css_analyzer.py        — CSS/SCSS @import detection
    docker_analyzer.py     — docker-compose + Dockerfile; service nodes
    ci_analyzer.py         — GitHub Actions + GitLab CI pipeline jobs
    schema_analyzer.py     — SQL + Prisma; table/model nodes
    cross_lang_analyzer.py — backend route ↔ frontend fetch/axios matching
    monorepo_analyzer.py   — pnpm/npm/yarn/Lerna/Nx/Turbo workspace detection

viz/
  explorer.html            — client-side D3 force graph + optional Three.js 3D
```

---

## 2. Parsing Pipeline

### 2.1 Language detection (`analyze.py:28–54`)

Scans repo root for indicator files (go.mod, package.json, Gemfile, etc.) and returns a list of detected language strings, e.g. `["python", "javascript", "docker"]`.

### 2.2 Analyzer dispatch (`analyze.py:67–79, 137–143`)

```python
_ANALYZERS = {
    "python":     python_analyzer,
    "javascript": js_analyzer,
    "go":         go_analyzer,
    ...
}
for lang in detected_langs:
    nodes, ext_nodes, links_map, meta = _ANALYZERS[lang].analyze(root, group_map)
```

### 2.3 Per-analyzer return type

All analyzers return the same 4-tuple:

```python
nodes: list[dict]  # repo-internal files
external_nodes: list[dict]  # external packages (type="import")
links_map: dict[tuple, int]  # {(source_id, target_id): weight}
meta: dict  # {"total_files": N, "total_loc": M, "framework": "..."}
```

### 2.4 Node schema (minimal)

| Field | Type | Notes |
|---|---|---|
| `id` | str | Repo-relative path or package identifier |
| `type` | str | module / config / component / hook / route / store / service / database / pipeline / import |
| `language` | str | python / javascript / go / ruby / rust / java / php / docker / sql / … |
| `size` | int | LOC estimate |
| `loc` | int | Actual line count |
| `group` | int | Directory grouping index |
| `imports` | int | Import statement count |
| `layer` | str | backend / frontend / infrastructure (added by `assign_layer()`) |

After enrichment, nodes also carry: `blast_score`, `direct_dependents`, `transitive_dependents`, `imports` (list of IDs), `imported_by` (list of IDs).

### 2.5 Result merging (`analyze.py:123–154`)

- Concatenate nodes + external_nodes from all analyzers; deduplicate by ID.
- Merge `links_map` entries (accumulate weights for same-pair keys).
- Call `assign_layer()` on each node (`analyze.py:82–94`).
- Call `detect_workspaces()` + `assign_packages()` for monorepo repos.
- Run `cross_lang_analyzer.find_api_boundaries()` to add `kind="api-call"` links.
- Return unified `{"nodes": [...], "links": [...], "meta": {...}}`.

---

## 3. Build Orchestrator (`index.py:13–34`)

```python
def build(repo_path, output):
    root = Path(repo_path).resolve()
    data = analyze(str(root))  # language detection + parsing
    blast = compute_blast_radius(data["nodes"], data["links"])  # impact.py:6–68
    enrich_nodes(data["nodes"], blast)  # impact.py:71–78 — mutates nodes
    enrich_links(
        data["nodes"], data["links"]
    )  # impact.py:81–99 — adds imports/imported_by
    data["meta"]["indexed"] = True
    dest = output or (root / INDEX_FILENAME)
    dest.write_text(json.dumps(data, indent=2))  # writes blastradius.json
```

`INDEX_FILENAME = "blastradius.json"` (index.py top).

---

## 4. Blast Radius (`impact.py:6–68`)

Algorithm: build a reverse adjacency map (target → set of sources), then BFS upward from each node to find all transitively dependent files.

```python
blast_score = direct_dependents + 0.5 * transitive_dependents
```

Result keyed by node ID:
```python
{
  "src/utils.py": {
    "direct_dependents": 2,
    "transitive_dependents": 5,
    "blast_score": 4.5,
    "direct_ids": [...],
    "transitive_ids": [...],
    "dep_paths": {dependent_id: [path_nodes...], ...}
  }
}
```

---

## 5. JSON Output

### 5.1 `blastradius.json` — written by `index.py:25`

```json
{
  "meta": {
    "root": "reponame/",
    "total_files": 42,
    "total_loc": 5000,
    "languages": ["python"],
    "framework": "fastapi",
    "indexed": true
  },
  "nodes": [
    {
      "id": "src/main.py",
      "type": "module",
      "language": "python",
      "size": 250,
      "loc": 250,
      "group": 0,
      "layer": "backend",
      "blast_score": 7.5,
      "direct_dependents": 3,
      "transitive_dependents": 12,
      "imports": ["src/utils.py", "fastapi"],
      "imported_by": ["src/api.py"]
    }
  ],
  "links": [
    {"source": "src/api.py", "target": "src/main.py", "weight": 2, "kind": "imports"}
  ]
}
```

### 5.2 `symbolindex.json` — written by `symbols.py:77–85`

```json
{
  "meta": {"generated": "2026-06-07", "repo": "blastradius/", "total_symbols": 156},
  "symbols": {
    "MyClass": [{"file": "src/main.py", "line": 42, "kind": "class", "exported": true, "methods": ["run"]}]
  },
  "file_symbols": {
    "src/main.py": [{"name": "MyClass", "line": 42, "kind": "class", "exported": true}]
  }
}
```

### 5.3 Symbol write paths

| Function | Location | Purpose |
|---|---|---|
| `write_standalone()` | `symbols.py:77–85` | Writes `symbolindex.json` |
| `write_inline()` | `symbols.py:88–114` | Merges symbol data into `blastradius.json` nodes |
| `write_claude_md()` | `symbols.py:146–171` | Upserts a symbol section into `CLAUDE.md` |

---

## 6. Symbol Extraction (`symbol_extractor.py`)

EXTRACTORS dispatch at line 322–338. Per-language strategies:

| Language | Strategy | Exports heuristic |
|---|---|---|
| Python | `ast` module | name not starting with `_` |
| JS/TS | Regex on `export` keyword | Lines starting with `export` |
| Go | Regex | CapitalCase names |
| Java/Kotlin | Regex + method scan | All (no visibility filter) |
| Rust | Regex | Lines with `pub` prefix |
| PHP | Regex | All |
| Ruby | Regex | All |

Symbol dict shape: `{name, file, line, kind, exported, [signature], [doc], [methods]}`.

---

## 7. CLI Dispatch (`cli.py`)

Entry point: `main()` at line 349. Argparse subparsers (line 265–346).

```python
dispatch = {
    "analyze": _cmd_analyze,  # line 9 — calls index.build(); --watch mode
    "impact": _cmd_impact,  # line 64 — blast report for one file
    "serve": _cmd_serve,  # line 125 — MCP or viz server
    "symbols": _cmd_symbols,  # line 140 — build symbol index
    "lookup": _cmd_lookup,  # line 172 — symbol lookup by name
    "dependencies": _cmd_dependencies,  # line 188 — imports/imported_by for a file
    "high-blast": _cmd_high_blast,  # line 226 — list files above threshold
    "install-hook": _cmd_install_hook,  # line 254 — git pre-commit hook
}
```

All query commands load the index via `index.load()` (reads JSON from disk).

---

## 8. MCP Server (`mcp_server.py`)

Protocol: JSON-RPC 2.0 over stdio, using the official Python MCP SDK v2.
`serve(repo_path=None)` accepts a fixed default repository without changing cwd.
Both legacy initialization and modern `2026-07-28` requests are supported.

**Registered tools** (explicit input schemas are validated before execution):

| Tool | Handler | Core call |
|---|---|---|
| `analyze_repo` | `_call_analyze_repo` | `index.build()` |
| `get_impact` | `_call_get_impact` | `compute_blast_radius()` |
| `get_dependencies` | `_call_get_dependencies` | `artifacts.index()` → node lookup |
| `get_high_blast_files` | `_call_get_high_blast_files` | `artifacts.index()` → filter |
| `lookup_symbol` | `_call_lookup_symbol` | explicit symbol JSON, otherwise SQLite with JSON fallback |
| `build_symbol_index` | `_call_build_symbol_index` | `symbols.build_symbol_index()` |
| `semantic_search` | `_call_semantic_search` | keyword + graph search, optional embeddings |
| `temporal_impact` | `_call_temporal_impact` | historical store query or selected repository's JSON graph |
| `graph_query` | `_call_graph_query` | store neighborhood query |
| `changed_since` | `_call_changed_since` | temporal store query + Git changes |

**Execution and resolution**:

- The SDK owns protocol routing, initialization/discovery, and cancellation.
  A narrow input adapter reports malformed frames and rejects invalid request IDs.
- Blocking tool work runs in a serialized worker; the protocol loop stays responsive.
- `artifacts.Artifacts` owns a call's artifact selection and database cleanup.
  Explicit paths win over `--repo`; cwd discovery is used only without `--repo`.
- `Artifacts.index_for_file()` keeps a selected JSON export independent of source
  path resolution. Absolute paths must match an exact node relative to the graph
  directory or configured/discovered repository context; conflicting matches fail.
  Detached exports require that context or a repo-relative query, not a suffix guess.
- `artifacts.resolve_file_id()` prefers exact paths and rejects ambiguous suffixes.
- Malformed calls and invalid arguments return protocol errors; execution failures
  use `isError: true`. Successful tools retain their existing JSON text payloads.
  Cancellation suppresses replies but does not roll back running writes.

---

## 9. Viz (`viz_server.py`, `blastradius/static/explorer.html`)

HTTP server using stdlib `http.server`. Routes:

| Route | Handler | Response |
|---|---|---|
| `/`, `/index.html` | `do_GET` | Serves packaged `blastradius/static/explorer.html` |
| `/graph` | `do_GET` (line 60) | Serves `blastradius.json` as JSON |
| `/refresh` | `do_GET` (line 65) | Calls `_run_analysis()` → `index.build()` |

Watch mode (`viz_server.py:73–109`): watchdog observer + 1 s debounce timer triggers `_run_analysis()` on file changes.

Client: D3.js force-directed graph + optional Three.js 3D. Reads `/graph` on load; re-fetches on `/refresh`.

---

## 10. Integration Seams for SQLite Store

These are the exact points where Phase 1 plugs in. No other files need to change initially.

### Seam A — Write path (`index.py:13–34`)

After `enrich_nodes()` + `enrich_links()` return, `data["nodes"]` and `data["links"]` are fully enriched. This is the canonical insertion point for upsert into SQLite.

```python
# index.py, after line 19:
enrich_links(data["nodes"], data["links"])
# ↑ INSERT SEAM: store.upsert(data, db_path)
data["meta"]["indexed"] = True
dest.write_text(json.dumps(data, indent=2))  # still write JSON for backward compat
```

### Seam B — Read path (`index.py:37–42`)

`load()` is the single read entry point used by every CLI command and MCP tool. Replacing this with a DB read propagates to all consumers automatically.

```python
def load(index_path: Path) -> dict:
    # Phase 1: keep JSON read, add DB as parallel write
    # Phase 1+: replace with store.export_as_dict(db_path)
    return json.loads(index_path.read_text())
```

Call sites: `_cmd_impact`, `_cmd_dependencies`, `_cmd_high_blast`, all MCP `_call_*` handlers.

### Seam C — Incremental re-parse trigger

`_cmd_analyze` (cli.py:9) calls `index.build()` unconditionally (full recompute). Phase 1 replaces this with:
1. `git diff --name-status <last_indexed_commit> HEAD` → changed file set
2. Re-parse only changed files
3. Upsert changed nodes/edges; close removed ones
4. Recompute blast only for the affected subgraph (transitive dependents of changed files)

The incremental logic lives in new `store/` and is called from `index.build()`, keeping the CLI interface unchanged.

### Seam D — Symbol write (`symbols.py:77–85`, `symbols.py:88–114`)

`write_standalone()` and `write_inline()` write symbolindex data. Phase 1 adds parallel writes to the `symbols` SQLite table and populates `symbols_fts`. The JSON files continue to be written as exports.

---

## 11. Data Flow Summary

```
cli.py main()
  └─ _cmd_analyze()
       └─ index.build(repo, output)
            ├─ analyze(root)                    ← language detection + all analyzers
            │    ├─ detect_languages()
            │    ├─ _ANALYZERS[lang].analyze()  ← per-lang: file scan → nodes/links
            │    ├─ assign_layer()
            │    ├─ detect_workspaces()
            │    └─ find_api_boundaries()
            ├─ compute_blast_radius()           ← BFS on reverse adjacency map
            ├─ enrich_nodes()                   ← mutate nodes with blast fields
            ├─ enrich_links()                   ← add imports/imported_by to nodes
            │                                   ← SEAM A: upsert to SQLite here
            └─ write blastradius.json

cli.py / mcp_server.py (query commands)
  └─ index.load(path)                           ← SEAM B: replace with DB read
       └─ json.loads(...)
```

---

## 12. Existing Tests

```
tests/
  test_cli.py              — integration tests for lookup, dependencies, high-blast
  (fixture repos checked in at tests/fixtures/ per design doc)
```

Run with `pytest`. All must stay green across every phase.

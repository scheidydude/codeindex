# blastradius

[Source repository](https://github.com/IWasZ3r0Cool/blast-radius) ·
[Issues](https://github.com/IWasZ3r0Cool/blast-radius/issues) ·
[PyPI package](https://pypi.org/project/blastradius-cli/)

**Temporal code knowledge graph** with blast-radius impact scoring, semantic symbol search, and git-history-aware dependency analysis — for AI-assisted development.

Point it at any project — Python, JavaScript/TypeScript, Go, Ruby, Rust, Java, PHP, and more — and get:

- A persistent **SQLite graph store** at `<repo>/.blastradius/index.db` — incremental, queryable, temporal
- A `blastradius.json` dependency index written directly into your repo (preserved for backward compatibility)
- Per-file blast-radius scores (how many files break if this one changes), including **historical as-of queries**
- A `symbolindex.json` symbol map so AI can find any function/class without scanning every file
- **Hybrid semantic search** over symbols: natural-language queries fused with keyword + graph expansion
- **Git history backfill** — temporal graph from commit history without touching the working tree
- Ten ways to consume the data: CLI, markdown report, MCP server (10 tools), pre-commit hook, CLAUDE.md injection
- An interactive visualization UI (2D/3D graphs, dependency matrix, treemap)

No project build step or npm required. SQLite is stdlib; the default installation
includes the official Python MCP SDK and its runtime dependencies.

---

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first, then
install the published tool into its own isolated environment:

```bash
uv tool install blastradius-cli
blastradius --help
```

The package is **`blastradius-cli`**; the executable is **`blastradius`**. This
repository maintains and publishes the PyPI package. You do not need to activate
a virtual environment or add blastradius to the project you want to analyze.
Python 3.10+ is required. MCP is included by default; no MCP extra is needed.
See uv's [tool guide](https://docs.astral.sh/uv/guides/tools/) for details.

If your shell cannot find `blastradius`, run `uv tool update-shell` and open a new
terminal. `uv tool dir --bin` shows the executable directory; use
`command -v blastradius` on macOS/Linux or `Get-Command blastradius` in PowerShell
to check which executable your shell finds. Remove an older installation with
its original installer if it shadows the uv-managed command.

### Run without a persistent installation

```bash
uvx --from blastradius-cli blastradius --help
uvx --from blastradius-cli blastradius analyze ./myapp
```

`uvx` is shorthand for `uv tool run`. Keep `--from blastradius-cli`: the package
name differs from the command name. For regular use, MCP clients, and git hooks,
prefer the persistent installation above.

### Upgrade or uninstall

```bash
uv tool upgrade blastradius-cli
uv tool list
uv tool uninstall blastradius-cli
```

Upgrades retain the source and version constraints used for installation.

### Install from this repository

PyPI contains the latest **published release**, not unreleased commits. To install
the default branch directly from this repository (requires Git):

```bash
uv tool install git+https://github.com/IWasZ3r0Cool/blast-radius.git
```

Or install a checkout, after selecting the branch or commit you want:

```bash
git clone https://github.com/IWasZ3r0Cool/blast-radius.git
cd blast-radius
uv tool install .
```

This installs a snapshot of the checkout. Re-run `uv tool install --reinstall .`
after changing it, or use `uv tool install --editable .` to intentionally track
local edits. Keep the checkout available for editable installs. Source installs
replace the same `blastradius-cli` tool; they are not a second executable.

### Develop and test blastradius itself

Inside this repository, use the project environment rather than the installed tool:

```bash
uv sync --extra dev
uv run blastradius --help
uv run pytest
uv run ruff check blastradius tests
uv build
```

The packaging test builds an sdist and wheel, installs the wheel with
`uv tool install` into temporary tool/bin directories, and exercises the installed
command outside the checkout. It does not modify your personal tool installation.

---

## Quickstart

After installation, run commands from the repository you want to analyze. Replace
the example file and release tag below with ones that exist in that repository.

```bash
cd ./myapp

# Build the dependency index (also writes to .blastradius/index.db)
blastradius analyze .

# Build the symbol index (where every function and class lives)
blastradius symbols .

# See blast radius for a file before touching it
blastradius impact src/auth.py

# Search symbols with natural language (no embedding endpoint needed — FTS fallback)
blastradius search "validate auth token"

# See what changed since a release tag
blastradius changed-since v1.2.0

# Launch the visualization UI
blastradius serve --viz --repo .
```

Leave the server running and visit **http://localhost:8080/** in your browser.
Use the HTTP URL, not the HTML file directly; the UI loads your index from `/graph`.

---

## Commands

### `blastradius analyze`

```bash
blastradius analyze [REPO_PATH] [--output PATH] [--watch]
```

Analyzes the repo and writes `blastradius.json` to the repo root. Detects 12+ languages automatically.

| Flag | Default | Description |
|------|---------|-------------|
| `REPO_PATH` | `.` | Path to repo root |
| `--output` | `<repo>/blastradius.json` | Override output path |
| `--watch` | off | Re-index on file changes (requires `watchdog`) |

---

### `blastradius symbols`

```bash
blastradius symbols [REPO_PATH] [--output PATH] [--inline] [--index PATH]
                  [--claude-md] [--claude-md-path PATH] [--all-symbols]
```

Builds a symbol index — a map of every function, class, struct, and type to its exact file and line number. Lets AI tools (and humans) find any symbol in one lookup instead of scanning the entire repo.

**Modes:**

| Flag | Description |
|------|-------------|
| _(none)_ | Write a standalone `symbolindex.json` |
| `--inline` | Embed symbols into each node in `blastradius.json` instead |
| `--claude-md` | Append a compressed symbol summary to `CLAUDE.md` |

Both `--inline` and `--claude-md` can be combined in a single run.

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `REPO_PATH` | `.` | Path to repo root |
| `--output` | `<repo>/symbolindex.json` | Output path (standalone mode) |
| `--index` | auto-discovered | Path to `blastradius.json` (for `--inline`) |
| `--claude-md-path` | `<repo>/CLAUDE.md` | Override CLAUDE.md path |
| `--all-symbols` | off | Include non-exported symbols in CLAUDE.md (default: exported only) |

**Examples:**

```bash
# Standalone symbol index
blastradius symbols ./myapp

# Embed into blastradius.json (one file for blast radius + symbols)
blastradius symbols ./myapp --inline

# Write CLAUDE.md summary so Claude Code loads symbols automatically
blastradius symbols ./myapp --claude-md

# All three at once
blastradius symbols ./myapp --inline --claude-md

# Re-generate when code changes
blastradius symbols ./myapp --inline --claude-md
```

**Why it matters:** Claude Code and other AI tools normally scan every file to find a function definition. With a symbol index, Claude can load one file, do an O(1) lookup, and open only the relevant file — cutting token usage 60–90% on symbol-location tasks.

**CLAUDE.md injection** is opt-in because it increases base context size on every prompt. Use it when symbol lookups are frequent in your workflow; skip it for simple tasks where the overhead outweighs the benefit.

---

### `blastradius impact`

```bash
blastradius impact FILE [--index PATH] [--out FILE] [--json] [--as-of REF]
```

Shows the blast-radius impact for a specific file: direct dependents, transitive dependents, blast score, and risk level.

```
Impact: src/auth.py
Blast Score: 8.5  (2 direct · 7 transitive)  [HIGH]

Direct dependents (2)
  src/api.py
  src/middleware.py

Transitive dependents (5 additional)
  src/main.py  ← src/api.py
  src/app.py   ← src/middleware.py
  ...

Risk: HIGH — affects 7/42 files (16.7% of codebase)
```

**Blast score formula:** `direct + (0.5 × transitive)`

| Flag | Description |
|------|-------------|
| `--index PATH` | Path to `blastradius.json` (auto-discovered if omitted) |
| `--out FILE` | Write a markdown report to this file |
| `--json` | Output raw JSON |
| `--as-of REF` | Compute blast radius at a historical commit/ref instead of HEAD |

---

### `blastradius search`

```bash
blastradius search QUERY [--k N] [--as-of REF] [--db PATH] [--json]
```

Hybrid semantic + keyword + graph symbol search. Finds relevant functions and classes without knowing their exact names.

**Retrieval signals fused with Reciprocal Rank Fusion (RRF):**
1. **Semantic KNN** — embedding similarity (requires `blastradius-cli[semantic]` + a configured endpoint)
2. **FTS5 keyword** — full-text search over symbol names, signatures, and docstrings
3. **Graph expansion** — structurally adjacent symbols from dependent/dependency files

Degrades gracefully: if no embedding endpoint is configured, falls back to FTS + graph (no crash, no config needed).

```bash
# Keyword + graph search (no embedding setup required)
blastradius search "validate auth token"

# Full semantic search (requires BLASTRADIUS_EMBEDDING_* env vars)
BLASTRADIUS_EMBEDDING_ENDPOINT=http://localhost:11434 \
BLASTRADIUS_EMBEDDING_MODEL=nomic-embed-text \
BLASTRADIUS_EMBEDDING_DIMS=768 \
blastradius search "validate auth token"

# Historical search — symbols visible at a release tag
blastradius search "token validation" --as-of v1.2.0
```

| Flag | Default | Description |
|------|---------|-------------|
| `QUERY` | — | Natural-language or keyword query |
| `--k N` | `10` | Number of results to return |
| `--as-of REF` | HEAD | Restrict to symbols visible at this commit/ref |
| `--db PATH` | auto-discovered | Path to `.blastradius/index.db` |
| `--json` | off | Output raw JSON |

---

### `blastradius history`

```bash
blastradius history [REPO_PATH] [--since REF] [--max-commits N] [--json]
```

Backfills temporal graph data from git history — without any working-tree checkouts. Reads blobs via `git cat-file --batch` and stamps `first_seen_commit` / `last_seen_commit` on every file, edge, and symbol.

Run this once after initial setup to enable `--as-of` queries across the full history.

```bash
# Backfill up to 1000 commits (default)
blastradius history .

# Backfill only recent history
blastradius history . --since v1.0.0 --max-commits 200
```

| Flag | Default | Description |
|------|---------|-------------|
| `REPO_PATH` | `.` | Path to repo root |
| `--since REF` | — | Only process commits after this date/ref |
| `--max-commits N` | `1000` | Maximum commits to process |
| `--json` | off | Output summary as JSON |

---

### `blastradius changed-since`

```bash
blastradius changed-since REF [--repo PATH] [--db PATH] [--json]
```

Lists files and dependency edges added or removed since a commit, branch, or tag.

```
$ blastradius changed-since v1.2.0
Changes since v1.2.0:

  Added files (2):
    + src/payments/stripe.py
    + src/payments/webhook.py

  Removed edges (1):
    - src/api.py → src/auth_v1.py  [imports]
```

| Flag | Default | Description |
|------|---------|-------------|
| `REF` | — | Commit hash, branch, or tag to compare against |
| `--repo PATH` | `.` | Repo root (for git operations) |
| `--db PATH` | auto-discovered | Path to `.blastradius/index.db` |
| `--json` | off | Output raw JSON |

---

### `blastradius db`

```bash
blastradius db status [--db PATH] [--json]
blastradius db migrate [--db PATH]
```

Manages the SQLite store at `<repo>/.blastradius/index.db`.

```
$ blastradius db status
schema_version      : 2
repo_root           : /Users/alice/myapp
last_indexed_commit : a3f2e1c8
active_files        : 142
active_edges        : 387
active_symbols      : 1204
embedding_model     : nomic-embed-text
embedding_dims      : 768
vec_symbols         : enabled
```

`db migrate` applies any pending schema migrations automatically (also runs on every `blastradius analyze`).

---

### `blastradius serve`

```bash
blastradius serve --viz [--repo PATH] [--port PORT] [--watch]
blastradius serve --mcp [--repo PATH]
```

`--viz` starts a local HTTP server for the interactive visualization UI (5 modes: 2D force graph, 3D network, dependency matrix, treemap, infrastructure graph). Visit the printed URL in your browser; keep the terminal process running. The installed tool includes the HTML, so no source checkout is needed. Use `--port` if port 8080 is already occupied.

`--mcp` starts a stdio MCP server that exposes blastradius tools directly to Claude and other MCP clients.

For a project-specific server, use either launcher:

```bash
blastradius serve --mcp --repo /path/to/project
uvx --from blastradius-cli blastradius serve --mcp --repo /path/to/project
```

The SDK supports modern MCP `2026-07-28` requests and legacy initialization with
`2024-11-05`, `2025-03-26`, `2025-06-18`, and `2025-11-25`. Modern clients use
`server/discover` and per-request metadata; discovery lists modern revisions.
Legacy clients continue to use `initialize` followed by `notifications/initialized`.
See the [MCP compatibility specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning).

`--repo` sets a fixed default repository; it does not analyze it automatically.
Explicit `index_path`, `symbol_index_path`, and `db_path` arguments take precedence.
Relative artifact and analysis paths are based on `--repo`, or on the startup
working directory when omitted. Without `--repo`, indexes are discovered by
walking up from that directory. With `--repo`, a missing index never falls back
to a parent or unrelated repository. Analyzing another repository does not change
this default. File queries prefer exact repo-relative or absolute paths; shortened
paths must identify a unique file.

For a graph exported outside its repository, keep the export selected with
`index_path` and configure `--repo /path/to/project` when querying absolute source
paths. `get_impact` and `get_dependencies` check exact node matches relative to
the graph's directory and the configured repository. Without `--repo`, repository
context comes from the nearest graph or database above the startup directory,
or that directory itself if neither exists. Conflicting matches are reported as
ambiguous. If no context matches, use a repo-relative file path or configure
`--repo`; absolute paths are never guessed by suffix. The selected graph's data
is never replaced, and its `meta.root` basename is not an absolute-location hint.

Tool arguments are validated without string-to-number coercion. `semantic_search.k`
must be a positive integer; `graph_query.depth` must be a nonnegative integer
(`0` returns the starting node only). Malformed calls, invalid arguments, and
unknown tools return protocol errors. Missing files/indexes and invalid Git refs
return failed tool results with actionable messages; an unknown symbol is a
successful lookup with `found: false`.

MCP uses stdin/stdout, not a browser URL. Diagnostics go to stderr. If a client
cannot connect, check the executable path, Python requirement, and client stderr.
If a tool cannot find its index, run `blastradius analyze /path/to/project`
(or `blastradius symbols /path/to/project` for a standalone symbol index) and
check `--repo` or its explicit artifact argument. Closing stdin shuts down the
server. Cancellation prevents queued work from starting and suppresses late
responses; an already-running synchronous write finishes safely, without rollback.

**MCP tools:**

| Tool | Description |
|------|-------------|
| `analyze_repo` | Build or refresh the dependency index |
| `get_impact` | Blast-radius report for a file |
| `get_dependencies` | imports + imported-by for a file |
| `get_high_blast_files` | All files above a blast score threshold |
| `build_symbol_index` | Build or refresh the symbol index |
| `lookup_symbol` | Find where any function/class/type is defined (file + line) |
| `semantic_search` | Hybrid semantic + keyword + graph symbol search; degrades gracefully without embeddings |
| `temporal_impact` | Blast-radius at a historical commit/ref (`as_of` parameter) |
| `graph_query` | k-hop dependency neighborhood of a file (`dependents`/`dependencies`/`both`) |
| `changed_since` | Files and edges added or removed since a commit/ref |

**Claude Code project MCP config** (`.mcp.json`):

```json
{
  "mcpServers": {
    "blastradius": {
      "command": "/absolute/path/to/blastradius",
      "args": ["serve", "--mcp", "--repo", "/absolute/path/to/project"]
    }
  }
}
```

Replace the command with the full path under `uv tool dir --bin` (append
`blastradius`, or `blastradius.exe` on Windows). See the setup walkthrough below.

---

### `blastradius lookup`

```bash
blastradius lookup SYMBOL [--index PATH] [--json]
```

Finds where a function, class, struct, or other symbol is defined. Queries the SQLite DB (same source as `search`), falling back to `symbolindex.json` if no DB is present. Prints 5 lines of source context around the definition.

```
$ blastradius lookup compute_blast_radius
blastradius/impact.py:6  compute_blast_radius  (function)

  >    6 | def compute_blast_radius(nodes: list[dict], links: list[dict]) -> dict[str, dict]:
         7 |     """..."""
         8 |     ...

$ blastradius lookup AuthService
src/auth.py:44  AuthService  (class)  methods: login, logout, refresh

  >   44 | class AuthService:
        45 |     def login(self, ...):
```

If a symbol isn't found, it's likely a third-party import — only symbols defined in the repo are indexed.

| Flag | Description |
|------|-------------|
| `--index PATH` | Path to `symbolindex.json` fallback (auto-discovered if omitted) |
| `--json` | Output raw JSON |

---

### `blastradius dependencies`

```bash
blastradius dependencies FILE [--index PATH] [--json]
```

Shows what a file imports and what imports it, plus its blast score.

```
$ blastradius dependencies src/auth.py
File: src/auth.py  (blast score: 8.5)

Imports (3):
  src/db.py
  src/config.py
  src/utils.py

Imported by (2):
  src/api.py
  src/middleware.py
```

| Flag | Description |
|------|-------------|
| `--index PATH` | Path to `blastradius.json` (auto-discovered if omitted) |
| `--json` | Output raw JSON |

---

### `blastradius high-blast`

```bash
blastradius high-blast [--threshold N] [--index PATH] [--json]
```

Lists all files whose blast score exceeds the threshold, sorted by score descending. Useful for identifying the riskiest files before a refactor.

```
$ blastradius high-blast --threshold 5
Files with blast score ≥ 5.0  (3 found)

  13.0  src/db.py          (12d / 2t)
   8.5  src/auth.py        (3d / 7t)
   5.5  src/config.py      (5d / 1t)
```

`d` = direct dependents · `t` = transitive dependents

| Flag | Default | Description |
|------|---------|-------------|
| `--threshold N` | `5` | Minimum blast score to include |
| `--index PATH` | auto-discovered | Path to `blastradius.json` |
| `--json` | off | Output raw JSON |

---

### `blastradius symbol-blast`

```bash
blastradius symbol-blast FILE [--json]
```

Per-export blast radius for a file. Lists every exported symbol with the count and exact file paths of importers that reference it by name. Useful when a file exports many symbols and you only need to touch one — lets you confirm the others are safe.

```
$ blastradius symbol-blast lib/db/schema.ts

Symbol-level blast radius: lib/db/schema.ts

  5 exported symbol(s), 12 importer(s)

  userSchema  (const, line 8)  →  8 user(s)
    app/api/users/route.ts
    app/api/profile/route.ts
    ...
  legacySchema  (const, line 42)  →  1 user(s)
    scripts/migrate.ts
  sessionSchema  (const, line 67)  →  3 user(s)
    ...
```

`userSchema` touches 8 routes; `legacySchema` touches only 1 — safe to change in isolation.

| Flag | Description |
|------|-------------|
| `FILE` | Repo-relative path to the file (e.g. `lib/db/schema.ts`) |
| `--json` | Output raw JSON with full `used_by` arrays per symbol |

---

### `blastradius install-hook`

```bash
blastradius install-hook [--repo PATH] [--threshold N] [--strict] [--remove]
```

Installs a git pre-commit hook that warns when staged files exceed the blast score threshold.

| Flag | Default | Description |
|------|---------|-------------|
| `--threshold N` | `10` | Blast score above which to warn |
| `--strict` | off | Block the commit instead of just warning |
| `--remove` | — | Uninstall the hook |

---

## Using blastradius in another repo with Claude

Three workflows, ordered by automation level.

### Workflow 1 — MCP server (recommended for active coding)

Claude gets symbol lookup, dependency, and impact tools it calls automatically. No extra prompting needed.

**One-time setup:**
```bash
cd /your/other/repo
blastradius analyze .
blastradius symbols .
```

Register the MCP server with Claude Code using `claude mcp add`. Use `--scope project`
for this repo, or `--scope user` for all your repos. These are the scopes in
[Claude Code's MCP documentation](https://code.claude.com/docs/en/mcp).

```bash
# Project-scoped (stored in .mcp.json)
claude mcp add --scope project blastradius -- /path/to/blastradius serve --mcp --repo /path/to/project

# User-scoped (available in all your repos)
claude mcp add --scope user blastradius -- /path/to/blastradius serve --mcp
```

Run `uv tool dir --bin`, append `/blastradius` (`\blastradius.exe` on Windows),
and substitute that absolute path above. On macOS/Linux, this can be done directly:

```bash
claude mcp add --scope user blastradius -- "$(uv tool dir --bin)/blastradius" serve --mcp
```

Verify it registered:
```bash
claude mcp list
```

> **Note:** A GUI or MCP client may have a different PATH from your terminal.
> An absolute executable path avoids relying on its shell configuration. Do not
> commit machine-specific executable or repository paths in shared `.mcp.json`
> without adapting them for your team. A user-scoped server without `--repo`
> relies on the client's launch directory; use explicit artifact paths when
> querying a different repository, or configure a project-specific server.

Claude now has all 10 MCP tools available in every session. When it needs to find `processPayment`, it calls `lookup_symbol("processPayment")` and gets `src/billing.py:142` back in one shot — no file scanning. When it needs to find code that validates auth tokens without knowing the exact name, it calls `semantic_search("validate auth token")`.

**Keep the index fresh:**
```bash
# Auto-rebuild the dependency index (requires the watch extra; leave running)
blastradius analyze . --watch

# Rebuild the standalone symbol index after edits, in another terminal
blastradius symbols .
```

`symbols` does not have a `--watch` flag.

---

### Workflow 2 — CLAUDE.md injection (best for repos you revisit often)

Symbol table is embedded in `CLAUDE.md` so it loads into every session automatically — no tool call needed at all.

```bash
cd /your/other/repo
blastradius symbols . --claude-md
```

This upserts a `symbolindex` code fence into `CLAUDE.md`. Every Claude Code session in that repo loads it at startup. Claude can answer "where is `X` defined?" from context alone with zero tool calls.

**Tradeoff:** adds ~500–2000 tokens to every prompt depending on repo size. Worth it for repos where symbol lookups are frequent; skip it for repos where you mostly write new code.

**Keep it fresh:**
```bash
# Re-run after significant refactors
blastradius analyze . && blastradius symbols . --claude-md
```

---

### Workflow 3 — Hybrid (large repos)

For large repos where the `--claude-md` section would be too large, use the MCP server for lookups and add a short hint to `CLAUDE.md` so Claude reaches for the tool first:

```bash
blastradius analyze .
blastradius symbols .
```

Then add to `CLAUDE.md`:
```markdown
## Blastradius
Symbol index: `symbolindex.json` — use the `lookup_symbol` MCP tool before grepping for any function or class.
Dependency index: `blastradius.json` — use `get_impact` before modifying high-blast files.
```

This costs almost no tokens but primes Claude to use the index rather than defaulting to grep.

---

### CLI quick reference for human use

The same data available via MCP is also accessible directly from the terminal:

```bash
# Find where a symbol is defined (shows code snippet)
blastradius lookup MyClassName
blastradius lookup process_payment --json

# Show what a file imports and what depends on it
blastradius dependencies src/auth.py

# List the riskiest files to change
blastradius high-blast --threshold 5

# Blast-radius report before touching a file
blastradius impact src/auth.py

# Per-export blast radius — which importers use each exported symbol
blastradius symbol-blast lib/db/schema.ts
```

---

### Which workflow to pick

| Situation | Workflow |
|-----------|----------|
| Daily driver repo, active feature work | MCP server |
| Medium repo, frequent symbol lookups | CLAUDE.md injection |
| Large repo (1000+ files) | MCP server + short CLAUDE.md hint |
| Quick one-off in an unfamiliar repo | `blastradius symbols . --claude-md`, delete after |
| Terminal / scripting use | CLI commands (`lookup`, `dependencies`, `high-blast`) |

---

## Supported Languages

| Language | Dependency analysis | Symbol extraction |
|----------|--------------------|--------------------|
| Python | AST imports, type detection | Functions, classes, methods (AST-precise) |
| JavaScript / TypeScript | ES modules, `require()`, framework detection | Exported functions, classes, types, enums, consts |
| Vue | SFC `<script>` imports | Exported symbols from `<script>` block |
| Go | Package-level nodes, `import` blocks | Functions, structs, interfaces (exported flag) |
| Ruby | `require`, `require_relative`, `autoload` | Classes, modules, methods |
| Rust | `mod`, `use crate::` | `pub fn`, structs, enums, traits |
| Java / Kotlin | FQN imports, wildcard imports | Classes, interfaces, methods |
| PHP | PSR-4 namespace resolution | Classes, interfaces, functions |
| CSS / SCSS / Less | `@import`, `@use`, `@forward` | — |
| Docker | Services, `depends_on` edges | — |
| CI/CD | GitHub Actions + GitLab CI jobs, `needs:` edges | — |
| SQL / Prisma | Tables/models, foreign key edges | — |

---

## Output schemas

### `blastradius.json`

```json
{
  "meta": {
    "root": "myapp/",
    "total_files": 60,
    "total_loc": 4085,
    "languages": ["python", "javascript"]
  },
  "nodes": [
    {
      "id": "src/auth.py",
      "type": "module",
      "language": "python",
      "layer": "backend",
      "loc": 142,
      "imports": ["src/db.py"],
      "imported_by": ["src/api.py", "src/middleware.py"],
      "direct_dependents": 2,
      "transitive_dependents": 7,
      "blast_score": 5.5,
      "symbols": [
        { "name": "verify_token", "line": 18, "kind": "function", "exported": true },
        { "name": "AuthService",  "line": 44, "kind": "class",    "exported": true,
          "methods": ["login", "logout", "refresh"] }
      ]
    }
  ],
  "links": [
    { "source": "src/api.py", "target": "src/auth.py", "weight": 1, "kind": "imports" }
  ]
}
```

The `symbols` field is only present when `blastradius symbols --inline` has been run.

---

### `symbolindex.json`

```json
{
  "meta": {
    "generated": "2026-05-21",
    "repo": "myapp/",
    "total_symbols": 312
  },
  "symbols": {
    "verify_token": [
      {
        "file": "src/auth.py",
        "line": 18,
        "kind": "function",
        "exported": true,
        "doc": "Verify a JWT and return the decoded payload."
      }
    ],
    "AuthService": [
      {
        "file": "src/auth.py",
        "line": 44,
        "kind": "class",
        "exported": true,
        "methods": ["login", "logout", "refresh"]
      }
    ]
  },
  "file_symbols": {
    "src/auth.py": [
      { "name": "verify_token", "line": 18, "kind": "function", "exported": true },
      { "name": "AuthService",  "line": 44, "kind": "class",    "exported": true,
        "methods": ["login", "logout", "refresh"] }
    ]
  }
}
```

**Lookup patterns:**

- *"Where is `verify_token` defined?"* → `symbols["verify_token"][0].file` + `.line` — O(1)
- *"What symbols live in `src/auth.py`?"* → `file_symbols["src/auth.py"]` — O(1)
- *"What's the blast radius of changing `verify_token`?"* → cross-reference `blastradius.json` via the file

---

### CLAUDE.md symbol section

When `--claude-md` is used, a compact section is upserted into `CLAUDE.md` bounded by HTML comment markers so re-runs update in place:

```
<!-- blastradius-symbols-start -->
## Symbol Index
_Generated by blastradius. Update: `blastradius symbols --claude-md`_

```symbolindex
src/auth.py: verify_token:fn:18 AuthService:cls:44[login,logout,refresh]
src/db.py: connect:fn:12 query:fn:28 close:fn:55
```
<!-- blastradius-symbols-end -->
```

Format per symbol: `name:kind_abbr:line[methods...]`
Kind abbreviations: `fn` function · `cls` class · `st` struct · `en` enum · `tr` trait · `if` interface · `ty` type · `co` const

---

## AI workflow comparison

| Task | Without blastradius | With symbolindex.json |
|------|-------------------|----------------------|
| Find where `process_payment` is defined | Grep / scan ~200 files | Load 1 file, O(1) lookup |
| Understand blast radius of a change | Manual tracing | `blastradius impact <file>` |
| Load only relevant context | Full repo scan | File + line from symbol map |
| Estimated token savings | baseline | **60–90% on symbol tasks** |

---

## Optional dependencies

The MCP SDK, AnyIO, and JSON Schema validation are required runtime dependencies
installed automatically. The following features remain optional:

| Package | Purpose | Install |
|---------|---------|---------|
| `sqlite-vec` | Semantic vector search in `blastradius search` and `semantic_search` MCP tool | `uv tool install --force 'blastradius-cli[semantic]'` |
| `watchdog` | `--watch` file change detection | `uv tool install --force 'blastradius-cli[watch]'` |
| `PyYAML` | Better Docker Compose / CI YAML parsing | `uv tool install --force 'blastradius-cli[yaml]'` |
| `tomli` | Rust `Cargo.toml` on Python < 3.11 | `uv tool install --force 'blastradius-cli[toml]'` |

These commands replace the installed tool's configuration with the requested
extras; they do not accumulate extras from previous commands. Combine everything
you need in one specification:

```bash
uv tool install --force 'blastradius-cli[semantic,watch,yaml,toml]'
```

The commands above install the published package. To retain a source checkout,
run `uv tool install --force '.[semantic,watch,yaml,toml]'` from that checkout.
Manage dependencies through `uv tool`, not by modifying its environment directly.
For contributor tests, use `uv sync --extra dev --extra semantic` instead.

### Semantic search configuration

Semantic search requires a self-hosted embedding endpoint (Ollama, LM Studio, llama.cpp server, or any OpenAI-compatible `/v1/embeddings` API) and the `sqlite-vec` extension.

```bash
uv tool install --force 'blastradius-cli[semantic]'

# Export in the shell that launches blastradius (or its MCP client)
export BLASTRADIUS_EMBEDDING_ENDPOINT=http://localhost:11434
export BLASTRADIUS_EMBEDDING_MODEL=nomic-embed-text
export BLASTRADIUS_EMBEDDING_DIMS=768

# Work in the repository to be indexed and searched
cd ./myapp
blastradius analyze .

# Search
blastradius search "validate JWT token"
```

The installed command does not automatically load `.env` files.

Without these env vars, `blastradius search` and the `semantic_search` MCP tool fall back to FTS5 keyword + graph search — no crash, no config required.

---

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) for the recommended tool installation
- Python 3.10+
- A modern browser (for `--viz` mode)

---

## License

Apache 2.0 — free to use and build on; attribution required in derivative works and documentation. Copyright 2026 David Scheiderman.

# AGENTS.md

## Developer Commands & Verification

- **Run unit tests**: `uv run pytest` (or single test: `uv run pytest tests/test_phase1.py` / `uv run pytest -k <name>`).
- **Run CLI integration benchmark**: `uv run python benchmark/test_cli.py` (auto-builds indexes if missing).
- **Run MCP integration benchmark**: `uv run python benchmark/test_mcp.py`.
- **MCP regression tests**: `uv run pytest tests/test_mcp_server.py`; subprocess tests cover modern and legacy protocol revisions, cancellation, and artifact selection.
- **Test local package build**: `uv build` (generates sdist & wheel in `dist/`).
- **Run CLI in dev**: `uv run blastradius <command>` or `uv run python -m blastradius.cli <command>`.
- **Environment note**: Prefer `uv run` or `.venv\Scripts\` execution over system python/pip.

## Core Architecture & Package Names

- **Canonical Repository**: `https://github.com/IWasZ3r0Cool/blast-radius` (use this repository for project, source, and issue links; preserve original copyright attribution).
- **PyPI Package Name**: `blastradius-cli` (published by this repository's maintainers; install with `uv tool install blastradius-cli`).
- **One-off tool use**: `uvx --from blastradius-cli blastradius <command>`; the package and executable names differ. Keep `uv run` for development in this checkout.
- **Python Import Package**: `blastradius` (source in `blastradius/`).
- **CLI Executable Command**: `blastradius` (entry point `blastradius.cli:main`).
- **MCP Runtime**: The official `mcp` v2 SDK is a default dependency. `serve --mcp --repo PATH` fixes the default repository; explicit artifact paths win. Do not import MCP transport code from ordinary CLI query helpers.
- **Index artifacts**: Default dependency graph is written to `blastradius.json`; SQLite store is populated at `<repo>/.blastradius/index.db`.
- **Layering rule**: `blastradius.store`, `blastradius.temporal`, and core modules must **never** import from `blastradius.semantic` or `blastradius.analyzers`.
- **Backward compatibility**: The `blastradius.json` file export schema is frozen. SQLite DB synchronization is an additive side-effect of `index.build()`.

## PyPI Release & CI Publishing (`.github/workflows/publish.yml`)

- **Publishing flow**: Automated via GitHub Actions using PyPI Trusted Publisher (OIDC).
- **Trigger**: Creating a GitHub Release or pushing a version tag (e.g. `git tag v0.3.7 && git push origin v0.3.7`).
- **PyPI Match constraint**: PyPI Pending/Trusted Publisher project name on PyPI.org must match `name = "blastradius-cli"` in `pyproject.toml`.

## Storage, Encoding & Database Gotchas (`blastradius/store/db.py`)

- **FTS5 table**: Must remain a standalone table without the `content=` option to prevent malformed disk image errors in WAL mode.
- **Soft deletes**: `sync()` must set `last_seen_commit` on soft-delete for temporal `--as-of` queries to filter correctly.
- **SQLite transactions**: `executescript()` auto-commits pending transactions in Python `sqlite3`. Keep regular DDL and FTS DDL in separate `executescript()` calls.
- **`sqlite-vec` optional dependency**: Never import `sqlite_vec` at module top-level. Wrap imports in `try...except` for runtime feature detection and graceful fallback to FTS5 + graph search.
- **Windows CLI Encoding**: Reconfigure `sys.stdout` and `sys.stderr` to `utf-8` on CLI entrypoints (`cli.py`, `test_cli.py`, `test_mcp.py`) to prevent Windows `cp1252` `UnicodeEncodeError`.

## Temporal Layer (`blastradius/temporal/history.py`)

- **Git history reading**: `backfill()` streams commit history using `git ls-tree` and `git cat-file --batch`. Do not replace this with `git checkout` or individual `git show` calls.

## Coding Conventions

- **Module header**: Every Python file starts with:
  ```python
  # Copyright 2026 David Scheiderman
  # Licensed under the Apache License, Version 2.0
  from __future__ import annotations
  ```
- **Python version**: Targets Python 3.10+. Keep `from __future__ import annotations` in every Python module.

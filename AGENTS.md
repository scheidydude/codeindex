# AGENTS.md

## Developer Commands & Verification

- **Run tests**: `uv run pytest` (set `$env:PYTHONPATH="."` if running outside an installed venv).
- **Run single test**: `uv run pytest tests/test_phase1.py` or `uv run pytest -k <test_name>`.
- **Run CLI in dev**: `uv run python -m blastradius.cli <command>` or `uv run blastradius <command>`.
- **Environment note**: Avoid system `pip` or system `python` if broken; prefer `uv run` or `.venv\Scripts\` execution.

## Core Architecture & Package Structure

- **Package name**: `blastradius` (source in `blastradius/`). Entry point: `blastradius.cli:main`.
- **Index artifacts**: Default dependency graph is written to `blastradius.json`; SQLite store is populated at `<repo>/.blastradius/index.db`.
- **Layering rule**: `blastradius.store`, `blastradius.temporal`, and core modules must **never** import from `blastradius.semantic` or `blastradius.analyzers`.
- **Backward compatibility**: The `blastradius.json` file export schema is frozen. SQLite DB synchronization is an additive side-effect of `index.build()`.

## Storage & Database Gotchas (`blastradius/store/db.py`)

- **FTS5 table**: Must remain a standalone table without the `content=` option. Using `content='symbols'` triggers `database disk image is malformed` errors during deletes in WAL mode.
- **Soft deletes**: `sync()` must set `last_seen_commit` on soft-delete for temporal `--as-of` queries to filter correctly.
- **SQLite transactions**: `executescript()` auto-commits pending transactions in Python `sqlite3`. Keep regular DDL and FTS DDL in separate `executescript()` calls.
- **`sqlite-vec` optional dependency**: Never import `sqlite_vec` at module top-level. Wrap imports in `try...except` for runtime feature detection and graceful fallback to FTS5 + graph search.

## Temporal Layer (`blastradius/temporal/history.py`)

- **Git history reading**: `backfill()` streams commit history using `git ls-tree` and `git cat-file --batch`. Do not replace this with `git checkout` or individual `git show` calls.

## Coding Conventions

- **Module header**: Every Python file starts with:
  ```python
  # Copyright 2026 David Scheiderman
  # Licensed under the Apache License, Version 2.0
  from __future__ import annotations
  ```
- **Python version**: Targets Python 3.9+. Modern union (`X | Y`) and generic types (`list[str]`) are enabled via `from __future__ import annotations`.

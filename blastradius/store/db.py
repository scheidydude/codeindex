# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
"""SQLite-backed persistent store for blastradius graph data.

Dependency rule: this module must not import from blastradius.graph or
blastradius.semantic. The dependency arrow points only upward into this layer.
"""
from __future__ import annotations

import re
import struct
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "3"

# sqlite-vec is an optional C extension; absence is a graceful degradation, not an error.
try:
    import sqlite_vec as _sqlite_vec
    _HAS_SQLITE_VEC = True
except ImportError:
    _sqlite_vec = None  # type: ignore[assignment]
    _HAS_SQLITE_VEC = False

_DDL = """
CREATE TABLE IF NOT EXISTS index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS commits (
    hash         TEXT PRIMARY KEY,
    authored_at  TEXT,
    committed_at TEXT,
    author       TEXT,
    message      TEXT,
    parent_hash  TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id                    INTEGER PRIMARY KEY,
    path                  TEXT NOT NULL,
    node_type             TEXT DEFAULT 'module',
    language              TEXT,
    layer                 TEXT,
    size                  INTEGER,
    loc                   INTEGER,
    node_group            INTEGER DEFAULT 0,
    imports_count         INTEGER DEFAULT 0,
    package               TEXT,
    content_hash          TEXT,
    blast_score           REAL DEFAULT 0.0,
    direct_dependents     INTEGER DEFAULT 0,
    transitive_dependents INTEGER DEFAULT 0,
    active                INTEGER NOT NULL DEFAULT 1,
    first_seen_commit     TEXT,
    last_seen_commit      TEXT,
    first_seen_at         TEXT,
    last_seen_at          TEXT,
    UNIQUE (path)
);

CREATE TABLE IF NOT EXISTS edges (
    id                INTEGER PRIMARY KEY,
    source_file_id    INTEGER NOT NULL REFERENCES files(id),
    target_file_id    INTEGER NOT NULL REFERENCES files(id),
    kind              TEXT NOT NULL,
    weight            REAL DEFAULT 1,
    active            INTEGER NOT NULL DEFAULT 1,
    first_seen_commit TEXT,
    last_seen_commit  TEXT,
    first_seen_at     TEXT,
    last_seen_at      TEXT,
    UNIQUE (source_file_id, target_file_id, kind)
);

CREATE TABLE IF NOT EXISTS symbols (
    id                INTEGER PRIMARY KEY,
    file_id           INTEGER NOT NULL REFERENCES files(id),
    name              TEXT NOT NULL,
    kind              TEXT,
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

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_file_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_name  ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file  ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_files_active  ON files(active);
"""

# FTS is created separately because executescript cannot mix DDL and virtual tables
# on all SQLite builds.
_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name, doc, signature,
    tokenize="porter unicode61"
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Persistent graph store backed by SQLite.

    Opens (or creates) the database at *db_path*, applies the schema, and
    exposes upsert / export / status operations.  All writes are transactional.
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._has_vec = False
        self._vec_dims: int | None = None
        self._apply_schema()
        self._maybe_load_vec()

    # ── schema ────────────────────────────────────────────────────────────────

    def _apply_schema(self) -> None:
        self._conn.executescript(_DDL)
        try:
            self._conn.executescript(_FTS_DDL)
        except sqlite3.OperationalError:
            # FTS5 not available on this SQLite build — degraded gracefully.
            pass
        existing = self.get_meta("schema_version")
        if existing is None:
            self.set_meta("schema_version", SCHEMA_VERSION)
        elif existing != SCHEMA_VERSION:
            self._migrate(existing)
        self._conn.commit()

    def _migrate(self, from_version: str) -> None:
        """Apply forward migrations. vec_symbols is created on demand via init_vectors()."""
        v = from_version
        if v == "1":
            self.set_meta("schema_version", "2")
            v = "2"
        if v == "2":
            # Recreate symbols_fts with Porter stemmer so "authentication" matches "auth*".
            try:
                self._conn.executescript("DROP TABLE IF EXISTS symbols_fts;")
                self._conn.executescript(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5("
                    "name, doc, signature, tokenize=\"porter unicode61\");"
                )
                self._conn.execute(
                    "INSERT INTO symbols_fts(rowid, name, doc, signature)"
                    " SELECT id, name, COALESCE(doc,''), COALESCE(signature,'')"
                    " FROM symbols WHERE active=1"
                )
            except sqlite3.OperationalError:
                pass  # FTS5 unavailable — degraded gracefully
            self.set_meta("schema_version", "3")
        self._conn.commit()

    def _maybe_load_vec(self) -> None:
        """Load sqlite-vec and activate vec_symbols if previously initialized."""
        if not _HAS_SQLITE_VEC:
            return
        dims_str = self.get_meta("embedding_dims")
        if not dims_str:
            return
        try:
            self._conn.enable_load_extension(True)
            _sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            self._vec_dims = int(dims_str)
            self._has_vec = True
        except Exception as exc:
            print(f"[blastradius] sqlite-vec load failed: {exc}", file=sys.stderr)
            self._has_vec = False

    # ── meta ──────────────────────────────────────────────────────────────────

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM index_meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO index_meta(key, value) VALUES (?, ?)",
            (key, value),
        )

    # ── sync ──────────────────────────────────────────────────────────────────

    def sync(
        self,
        data: dict,
        commit: str | None = None,
        changed_paths: set | None = None,
    ) -> None:
        """Upsert all nodes/edges from a fully-enriched graph dict.

        *changed_paths* is informational: when provided it is logged so callers
        can assert that only the expected change set was processed.  The full
        graph is always written because Phase-1 parsers scan the whole repo;
        per-file parsing is a future optimisation.
        """
        now = _now()
        seen_file_ids: list[int] = []
        node_id_map: dict[str, int] = {}  # path -> rowid

        for node in data["nodes"]:
            path = node["id"]
            imports = node.get("imports", [])
            imports_count = len(imports) if isinstance(imports, list) else int(imports or 0)

            row = self._conn.execute(
                "SELECT id FROM files WHERE path = ?", (path,)
            ).fetchone()

            if row:
                fid = row[0]
                self._conn.execute(
                    """UPDATE files SET
                        node_type=?, language=?, layer=?, size=?, loc=?,
                        node_group=?, imports_count=?, package=?, content_hash=?,
                        blast_score=?, direct_dependents=?, transitive_dependents=?,
                        active=1, last_seen_commit=?, last_seen_at=?
                       WHERE id=?""",
                    (
                        node.get("type", "module"),
                        node.get("language"),
                        node.get("layer"),
                        node.get("size"),
                        node.get("loc"),
                        node.get("group", 0),
                        imports_count,
                        node.get("package"),
                        node.get("content_hash"),
                        node.get("blast_score", 0.0),
                        node.get("direct_dependents", 0),
                        node.get("transitive_dependents", 0),
                        commit,
                        now,
                        fid,
                    ),
                )
            else:
                cur = self._conn.execute(
                    """INSERT INTO files(
                        path, node_type, language, layer, size, loc,
                        node_group, imports_count, package, content_hash,
                        blast_score, direct_dependents, transitive_dependents,
                        active, first_seen_commit, last_seen_commit,
                        first_seen_at, last_seen_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                    (
                        path,
                        node.get("type", "module"),
                        node.get("language"),
                        node.get("layer"),
                        node.get("size"),
                        node.get("loc"),
                        node.get("group", 0),
                        imports_count,
                        node.get("package"),
                        node.get("content_hash"),
                        node.get("blast_score", 0.0),
                        node.get("direct_dependents", 0),
                        node.get("transitive_dependents", 0),
                        commit,
                        commit,
                        now,
                        now,
                    ),
                )
                fid = cur.lastrowid  # type: ignore[assignment]

            node_id_map[path] = fid
            seen_file_ids.append(fid)

        # Soft-delete files absent from current graph; record the commit where they disappeared
        if seen_file_ids:
            placeholders = ",".join("?" * len(seen_file_ids))
            self._conn.execute(
                f"UPDATE files SET active=0, last_seen_commit=?, last_seen_at=?"
                f" WHERE active=1 AND id NOT IN ({placeholders})",
                [commit, now] + seen_file_ids,
            )
        else:
            self._conn.execute(
                "UPDATE files SET active=0, last_seen_commit=?, last_seen_at=? WHERE active=1",
                (commit, now),
            )

        # Sync edges: upsert current links, then soft-delete removed ones
        seen_edge_ids: list[int] = []
        for link in data["links"]:
            s_fid = node_id_map.get(link["source"])
            t_fid = node_id_map.get(link["target"])
            if s_fid is None or t_fid is None:
                continue
            kind = link.get("kind", "imports")
            weight = link.get("weight", 1)

            row = self._conn.execute(
                "SELECT id FROM edges WHERE source_file_id=? AND target_file_id=? AND kind=?",
                (s_fid, t_fid, kind),
            ).fetchone()

            if row:
                eid = row[0]
                self._conn.execute(
                    """UPDATE edges SET weight=?, active=1,
                        last_seen_commit=?, last_seen_at=?
                       WHERE id=?""",
                    (weight, commit, now, eid),
                )
            else:
                cur = self._conn.execute(
                    """INSERT INTO edges(
                        source_file_id, target_file_id, kind, weight, active,
                        first_seen_commit, last_seen_commit, first_seen_at, last_seen_at
                    ) VALUES (?,?,?,?,1,?,?,?,?)""",
                    (s_fid, t_fid, kind, weight, commit, commit, now, now),
                )
                eid = cur.lastrowid  # type: ignore[assignment]

            seen_edge_ids.append(eid)

        if seen_edge_ids:
            placeholders = ",".join("?" * len(seen_edge_ids))
            self._conn.execute(
                f"UPDATE edges SET active=0, last_seen_commit=?, last_seen_at=?"
                f" WHERE active=1 AND id NOT IN ({placeholders})",
                [commit, now] + seen_edge_ids,
            )
        else:
            self._conn.execute(
                "UPDATE edges SET active=0, last_seen_commit=?, last_seen_at=? WHERE active=1",
                (commit, now),
            )

        self._conn.commit()

    def sync_symbols(
        self,
        symbol_data: dict,
        commit: str | None = None,
    ) -> None:
        """Upsert symbols from build_symbol_index() output and refresh FTS."""
        now = _now()
        seen_symbol_ids: list[int] = []

        for rel_path, syms in symbol_data.get("file_symbols", {}).items():
            row = self._conn.execute(
                "SELECT id FROM files WHERE path=? AND active=1", (rel_path,)
            ).fetchone()
            if not row:
                continue
            fid = row[0]

            for sym in syms:
                existing = self._conn.execute(
                    "SELECT id FROM symbols WHERE file_id=? AND name=?",
                    (fid, sym["name"]),
                ).fetchone()

                if existing:
                    sid = existing[0]
                    self._conn.execute(
                        """UPDATE symbols SET kind=?, line=?, exported=?,
                            signature=?, doc=?, active=1,
                            last_seen_commit=?, last_seen_at=?
                           WHERE id=?""",
                        (
                            sym.get("kind"),
                            sym.get("line"),
                            1 if sym.get("exported") else 0,
                            sym.get("signature"),
                            sym.get("doc"),
                            commit,
                            now,
                            sid,
                        ),
                    )
                else:
                    cur = self._conn.execute(
                        """INSERT INTO symbols(
                            file_id, name, kind, line, exported,
                            signature, doc, active,
                            first_seen_commit, last_seen_commit,
                            first_seen_at, last_seen_at
                        ) VALUES (?,?,?,?,?,?,?,1,?,?,?,?)""",
                        (
                            fid,
                            sym["name"],
                            sym.get("kind"),
                            sym.get("line"),
                            1 if sym.get("exported") else 0,
                            sym.get("signature"),
                            sym.get("doc"),
                            commit,
                            commit,
                            now,
                            now,
                        ),
                    )
                    sid = cur.lastrowid  # type: ignore[assignment]

                seen_symbol_ids.append(sid)

        if seen_symbol_ids:
            placeholders = ",".join("?" * len(seen_symbol_ids))
            self._conn.execute(
                f"UPDATE symbols SET active=0, last_seen_at=? WHERE active=1 AND id NOT IN ({placeholders})",
                [now] + seen_symbol_ids,
            )
        else:
            self._conn.execute(
                "UPDATE symbols SET active=0, last_seen_at=? WHERE active=1", (now,)
            )

        # Refresh FTS: repopulate with explicit rowid = symbol id so FTS rowid → symbols.id.
        # Uses standalone FTS5 table (no content=) — DELETE works normally in WAL mode.
        try:
            self._conn.execute("DELETE FROM symbols_fts")
            self._conn.execute(
                """INSERT INTO symbols_fts(rowid, name, doc, signature)
                   SELECT id, name, COALESCE(doc,''), COALESCE(signature,'')
                   FROM symbols WHERE active=1"""
            )
        except sqlite3.OperationalError:
            pass  # FTS5 not available on this build

        self._conn.commit()

    # ── temporal ──────────────────────────────────────────────────────────────

    def record_commit(self, commit: dict) -> None:
        """Insert a commit record (idempotent)."""
        self._conn.execute(
            """INSERT OR IGNORE INTO commits(
                hash, authored_at, committed_at, author, message, parent_hash
            ) VALUES (?,?,?,?,?,?)""",
            (
                commit.get("hash"),
                commit.get("authored_at"),
                commit.get("committed_at"),
                commit.get("author"),
                commit.get("message"),
                commit.get("parent_hash"),
            ),
        )

    def apply_file_temporal(
        self,
        file_first_seen: dict,
        file_last_seen_alive: dict,
    ) -> None:
        """Bulk-update first_seen_commit on files from history backfill.

        Only sets first_seen_commit where it is currently NULL — we never
        overwrite a value already recorded by analyze().  last_seen_commit for
        active files is left alone; for files absent from the current HEAD that
        we find in backfill, we insert a skeleton row.
        """
        now = _now()
        for path, first_hash in file_first_seen.items():
            # Insert skeleton if the file has never been seen by analyze()
            self._conn.execute(
                """INSERT OR IGNORE INTO files(
                    path, active, first_seen_commit, first_seen_at, last_seen_at
                ) VALUES (?,0,?,?,?)""",
                (path, first_hash, now, now),
            )
            # Always overwrite first_seen_commit from history — analyze() only knows HEAD,
            # but history walks back to the true first commit for each file.
            self._conn.execute(
                "UPDATE files SET first_seen_commit=? WHERE path=?",
                (first_hash, path),
            )
        self._conn.commit()

    def apply_edge_temporal(
        self,
        edge_first_seen: dict,
        edge_last_seen_alive: dict,
    ) -> None:
        """Bulk-update first_seen_commit on edges from history backfill.

        Edges are identified by (source_path, target_path, kind).  Only sets
        first_seen_commit where NULL; does not overwrite existing values.
        """
        now = _now()
        for (src_path, tgt_path, kind), first_hash in edge_first_seen.items():
            src_row = self._conn.execute(
                "SELECT id FROM files WHERE path=?", (src_path,)
            ).fetchone()
            tgt_row = self._conn.execute(
                "SELECT id FROM files WHERE path=?", (tgt_path,)
            ).fetchone()
            if not src_row or not tgt_row:
                continue
            s_fid, t_fid = src_row[0], tgt_row[0]

            self._conn.execute(
                """INSERT OR IGNORE INTO edges(
                    source_file_id, target_file_id, kind, weight, active,
                    first_seen_commit, first_seen_at, last_seen_at
                ) VALUES (?,?,?,1,0,?,?,?)""",
                (s_fid, t_fid, kind, first_hash, now, now),
            )
            # Update ALL edge kinds between this pair — the backfill only tracks "imports"
            # but the analyzer may record the same relationship as "renders"/"styles"/"depends".
            # The historical first-seen time applies regardless of how the edge is classified.
            self._conn.execute(
                """UPDATE edges SET first_seen_commit=?
                   WHERE source_file_id=? AND target_file_id=?""",
                (first_hash, s_fid, t_fid),
            )
        self._conn.commit()

    def _populate_reachable_temp(self, reachable: set) -> None:
        """Populate _reachable temp table for as-of / changed-since queries."""
        self._conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _reachable(hash TEXT PRIMARY KEY)"
        )
        self._conn.execute("DELETE FROM _reachable")
        self._conn.executemany(
            "INSERT OR IGNORE INTO _reachable(hash) VALUES(?)",
            [(h,) for h in reachable],
        )

    def as_of_impact(self, file_path: str, reachable: set) -> dict | None:
        """Compute blast radius for file_path at the historical point defined by reachable.

        *reachable* is the set of commit hashes reachable from the --as-of ref,
        obtained via `git log --format=%H <ref>`.
        Returns None if no temporal data is available.
        """
        from blastradius.impact import compute_blast_radius  # avoid circular at module level

        self._populate_reachable_temp(reachable)

        # Files active at this historical point:
        # first_seen in reachable AND (still active at HEAD OR removed after ref)
        file_rows = self._conn.execute("""
            SELECT id, path FROM files
            WHERE first_seen_commit IN (SELECT hash FROM _reachable)
            AND (
                active = 1
                OR (active = 0
                    AND last_seen_commit IS NOT NULL
                    AND last_seen_commit NOT IN (SELECT hash FROM _reachable))
            )
        """).fetchall()

        if not file_rows:
            return None

        nodes = [{"id": r[1]} for r in file_rows]
        file_id_set = {r[0] for r in file_rows}

        # Edges active at this historical point
        edge_rows = self._conn.execute("""
            SELECT f1.path, f2.path
            FROM edges e
            JOIN files f1 ON e.source_file_id = f1.id
            JOIN files f2 ON e.target_file_id = f2.id
            WHERE e.first_seen_commit IN (SELECT hash FROM _reachable)
            AND (
                e.active = 1
                OR (e.active = 0
                    AND e.last_seen_commit IS NOT NULL
                    AND e.last_seen_commit NOT IN (SELECT hash FROM _reachable))
            )
        """).fetchall()

        links = [{"source": r[0], "target": r[1]} for r in edge_rows]

        blast_map = compute_blast_radius(nodes, links)
        return blast_map.get(file_path)

    def _backfill_warning(self) -> str | None:
        """Return a warning string if git history hasn't been backfilled.

        Heuristic: if all active files share exactly one distinct first_seen_commit,
        temporal data comes from a single analyze run — history was never backfilled.
        changed_since results against any ref older than that run will be inaccurate.
        """
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT first_seen_commit) FROM files WHERE active=1"
        ).fetchone()
        if row and row[0] <= 1:
            return (
                "Git history has not been backfilled — all files share the same "
                "first_seen_commit. Run `blastradius history` first for accurate "
                "changed_since results."
            )
        return None

    def changed_since(self, reachable: set) -> dict:
        """List files/edges added or removed since the historical point defined by reachable."""
        self._populate_reachable_temp(reachable)

        warning = self._backfill_warning()

        _source_types = "('module','component','hook','store','route','config')"

        added_files = [
            r[0] for r in self._conn.execute(f"""
                SELECT path FROM files
                WHERE active = 1
                AND first_seen_commit IS NOT NULL
                AND first_seen_commit NOT IN (SELECT hash FROM _reachable)
                AND COALESCE(node_type,'module') IN {_source_types}
            """).fetchall()
        ]

        removed_files = [
            r[0] for r in self._conn.execute(f"""
                SELECT path FROM files
                WHERE active = 0
                AND last_seen_commit IS NOT NULL
                AND last_seen_commit NOT IN (SELECT hash FROM _reachable)
                AND COALESCE(node_type,'module') IN {_source_types}
            """).fetchall()
        ]

        added_edges = [
            {"source": r[0], "target": r[1], "kind": r[2], "first_seen_commit": r[3]}
            for r in self._conn.execute(f"""
                SELECT f1.path, f2.path, e.kind, e.first_seen_commit
                FROM edges e
                JOIN files f1 ON e.source_file_id = f1.id
                JOIN files f2 ON e.target_file_id = f2.id
                WHERE e.active = 1
                AND e.first_seen_commit IS NOT NULL
                AND e.first_seen_commit NOT IN (SELECT hash FROM _reachable)
                AND COALESCE(f1.node_type,'module') IN {_source_types}
                AND COALESCE(f2.node_type,'module') IN {_source_types}
            """).fetchall()
        ]

        removed_edges = [
            {"source": r[0], "target": r[1], "kind": r[2]}
            for r in self._conn.execute(f"""
                SELECT f1.path, f2.path, e.kind
                FROM edges e
                JOIN files f1 ON e.source_file_id = f1.id
                JOIN files f2 ON e.target_file_id = f2.id
                WHERE e.active = 0
                AND e.last_seen_commit IS NOT NULL
                AND e.last_seen_commit NOT IN (SELECT hash FROM _reachable)
                AND COALESCE(f1.node_type,'module') IN {_source_types}
                AND COALESCE(f2.node_type,'module') IN {_source_types}
            """).fetchall()
        ]

        result: dict = {
            "added_files":   added_files,
            "removed_files": removed_files,
            "added_edges":   added_edges,
            "removed_edges": removed_edges,
        }
        if warning:
            result["warning"] = warning
        return result

    # ── semantic / vector ─────────────────────────────────────────────────────

    def init_vectors(self, dims: int) -> bool:
        """Create vec_symbols virtual table (sqlite-vec required).

        Safe to call multiple times; uses CREATE VIRTUAL TABLE IF NOT EXISTS.
        Returns True if the table is ready, False if sqlite-vec is unavailable.
        """
        if not _HAS_SQLITE_VEC:
            print(
                "[blastradius] sqlite-vec not installed — semantic search unavailable. "
                "Install with: pip install 'blastradius[semantic]'",
                file=sys.stderr,
            )
            return False
        try:
            if not self._has_vec:
                self._conn.enable_load_extension(True)
                _sqlite_vec.load(self._conn)
                self._conn.enable_load_extension(False)
            # DDL must be a string literal with dims baked in
            self._conn.executescript(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_symbols USING vec0("
                f"symbol_id INTEGER PRIMARY KEY, embedding FLOAT[{dims}]);"
            )
            self._vec_dims = dims
            self._has_vec = True
            self.set_meta("embedding_dims", str(dims))
            self._conn.commit()
            return True
        except Exception as exc:
            print(f"[blastradius] sqlite-vec init failed: {exc}", file=sys.stderr)
            self._has_vec = False
            return False

    def upsert_embeddings(self, vecs: list[tuple[int, list[float]]]) -> None:
        """Store embeddings for symbol IDs into vec_symbols.

        *vecs* is a list of (symbol_id, float_list) pairs.
        Silently no-ops if vec_symbols is not initialized.
        """
        if not self._has_vec or not vecs:
            return
        for sid, vec in vecs:
            blob = struct.pack(f"{len(vec)}f", *vec)
            self._conn.execute(
                "INSERT OR REPLACE INTO vec_symbols(symbol_id, embedding) VALUES (?, ?)",
                (sid, blob),
            )
        self._conn.commit()

    def semantic_search(self, query_vec: list[float], k: int) -> list[int]:
        """KNN search over vec_symbols. Returns symbol IDs ordered by distance.

        Caller must check _has_vec before calling.
        """
        blob = struct.pack(f"{len(query_vec)}f", *query_vec)
        rows = self._conn.execute(
            "SELECT symbol_id, distance FROM vec_symbols "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (blob, k),
        ).fetchall()
        return [r[0] for r in rows]

    def fts_search(self, query: str, k: int) -> list[int]:
        """Full-text search over symbols_fts. Returns symbol IDs ordered by relevance.

        The FTS rowid equals symbols.id (set explicitly in sync_symbols).
        Multi-word queries use AND semantics first; if that returns nothing, retries
        with OR so "auth token" finds symbols containing either word.
        Returns empty list if FTS is unavailable or the query is empty.
        """
        if not query.strip():
            return []
        # Strip characters that are FTS5 syntax (", *, ^, -, (, ))
        # so raw user queries don't cause OperationalError.
        safe = re.sub(r'["\*\^\-\(\)]', ' ', query).strip()
        if not safe:
            return []
        words = safe.split()
        try:
            # Primary: prefix-OR with Porter stemmer handles most word variants.
            # "auth login" matches "authenticate", "loginAction", etc.
            prefix_or = " OR ".join(w + "*" for w in words)
            rows = self._conn.execute(
                "SELECT rowid FROM symbols_fts WHERE symbols_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (prefix_or, k),
            ).fetchall()
            if rows:
                return [r[0] for r in rows]

            # Truncation fallback: bridges abbreviation-to-full-word gaps that
            # stemming can't handle, e.g. "authentication" → "auth*".
            # Only runs when the primary pass found nothing.
            trunc_parts = []
            for w in words:
                if len(w) >= 8:
                    trunc_parts.append(w[: max(4, len(w) * 3 // 4)] + "*")
                if len(w) > 4:
                    trunc_parts.append(w[:4] + "*")
            if trunc_parts:
                trunc_query = " OR ".join(dict.fromkeys(trunc_parts))
                rows = self._conn.execute(
                    "SELECT rowid FROM symbols_fts WHERE symbols_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (trunc_query, k),
                ).fetchall()
                return [r[0] for r in rows]

            return []
        except sqlite3.OperationalError:
            return []

    def graph_expand(self, symbol_ids: list[int], k: int) -> list[int]:
        """Return symbol IDs from files structurally adjacent to the given symbols.

        Finds files that import or are imported by the files containing the input
        symbols, then returns active symbols from those related files (excluding
        symbols already in symbol_ids).
        """
        if not symbol_ids:
            return []
        ph = ",".join("?" * len(symbol_ids))
        file_rows = self._conn.execute(
            f"SELECT DISTINCT file_id FROM symbols WHERE id IN ({ph}) AND active=1",
            symbol_ids,
        ).fetchall()
        if not file_rows:
            return []
        file_ids = [r[0] for r in file_rows]
        fph = ",".join("?" * len(file_ids))
        related = self._conn.execute(
            f"""SELECT DISTINCT fid FROM (
                SELECT source_file_id AS fid FROM edges
                WHERE target_file_id IN ({fph}) AND active=1
                UNION
                SELECT target_file_id AS fid FROM edges
                WHERE source_file_id IN ({fph}) AND active=1
            ) WHERE fid NOT IN ({fph})""",
            file_ids + file_ids + file_ids,
        ).fetchall()
        if not related:
            return []
        related_ids = [r[0] for r in related]
        rph = ",".join("?" * len(related_ids))
        eph = ",".join("?" * len(symbol_ids))
        sym_rows = self._conn.execute(
            f"SELECT id FROM symbols WHERE file_id IN ({rph}) AND active=1 "
            f"AND id NOT IN ({eph}) LIMIT ?",
            related_ids + symbol_ids + [k],
        ).fetchall()
        return [r[0] for r in sym_rows]

    def symbol_visible_at(self, symbol_id: int, reachable: set) -> bool:
        """Return True if the symbol was present at the historical point given by reachable."""
        row = self._conn.execute(
            "SELECT first_seen_commit, last_seen_commit, active FROM symbols WHERE id=?",
            (symbol_id,),
        ).fetchone()
        if not row:
            return False
        first, last, active = row[0], row[1], row[2]
        if first and first not in reachable:
            return False
        if active == 0 and last and last not in reachable:
            return False
        return True

    def get_symbol(self, symbol_id: int) -> dict | None:
        """Return symbol metadata dict or None if not found."""
        row = self._conn.execute(
            """SELECT s.id, s.name, s.kind, s.line, s.exported, s.signature, s.doc,
                      f.path AS file, f.blast_score
               FROM symbols s
               JOIN files f ON s.file_id = f.id
               WHERE s.id = ?""",
            (symbol_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id":          row["id"],
            "name":        row["name"],
            "kind":        row["kind"],
            "line":        row["line"],
            "exported":    bool(row["exported"]),
            "signature":   row["signature"],
            "doc":         row["doc"],
            "file":        row["file"],
            "blast_score": row["blast_score"],
        }

    def lookup_by_name(self, name: str) -> list[dict]:
        """Return all active symbols with the given name."""
        rows = self._conn.execute(
            """SELECT s.id, s.name, s.kind, s.line, s.exported, s.signature, s.doc,
                      f.path AS file, f.blast_score
               FROM symbols s
               JOIN files f ON s.file_id = f.id
               WHERE s.name = ? AND s.active = 1""",
            (name,),
        ).fetchall()
        return [
            {
                "id":          r["id"],
                "name":        r["name"],
                "kind":        r["kind"],
                "line":        r["line"],
                "exported":    bool(r["exported"]),
                "signature":   r["signature"],
                "doc":         r["doc"],
                "file":        r["file"],
                "blast_score": r["blast_score"],
            }
            for r in rows
        ]

    def exported_symbols_for_file(self, file_path: str) -> list[dict]:
        """Return all active exported symbols for a repo-relative file path."""
        rows = self._conn.execute(
            """SELECT s.id, s.name, s.kind, s.line, s.signature, s.doc
               FROM symbols s
               JOIN files f ON s.file_id = f.id
               WHERE f.path = ? AND s.exported = 1 AND s.active = 1
               ORDER BY s.line""",
            (file_path,),
        ).fetchall()
        return [
            {"id": r[0], "name": r[1], "kind": r[2], "line": r[3],
             "signature": r[4], "doc": r[5]}
            for r in rows
        ]

    def importers_of_file(self, file_path: str) -> list[str]:
        """Return repo-relative paths of all active files that import file_path."""
        rows = self._conn.execute(
            """SELECT f.path FROM files f
               JOIN edges e ON e.source_file_id = f.id
               JOIN files t ON e.target_file_id = t.id
               WHERE t.path = ? AND e.active = 1 AND f.active = 1""",
            (file_path,),
        ).fetchall()
        return [r[0] for r in rows]

    def symbols_needing_embeddings(self) -> list[tuple[int, str]]:
        """Return (symbol_id, text) pairs for active symbols without a vec_symbols entry.

        Text is `name + ' ' + signature + ' ' + doc` (empty parts omitted).
        Used by index.py to embed only new/changed symbols incrementally.
        """
        if not self._has_vec:
            return []
        rows = self._conn.execute(
            """SELECT s.id, s.name, COALESCE(s.signature,''), COALESCE(s.doc,'')
               FROM symbols s
               LEFT JOIN vec_symbols v ON v.symbol_id = s.id
               WHERE s.active=1 AND v.symbol_id IS NULL"""
        ).fetchall()
        result = []
        for r in rows:
            parts = [p for p in [r[1], r[2], r[3]] if p]
            result.append((r[0], " ".join(parts)))
        return result

    def neighborhood(self, file_path: str, direction: str, depth: int) -> dict:
        """Return k-hop neighborhood of a file for graph_query MCP tool.

        *direction* is "dependents" (who imports this file), "dependencies"
        (what this file imports), or "both".  Returns all reachable files within
        *depth* hops, annotated with hop distance from the center.
        """
        start = self._conn.execute(
            "SELECT id FROM files WHERE path=? AND active=1", (file_path,)
        ).fetchone()
        if not start:
            return {"error": f"File not found in active index: {file_path}"}

        visited: dict[int, int] = {start[0]: 0}
        frontier = [start[0]]

        for d in range(1, depth + 1):
            next_frontier: list[int] = []
            for fid in frontier:
                if direction in ("dependents", "both"):
                    rows = self._conn.execute(
                        "SELECT source_file_id FROM edges WHERE target_file_id=? AND active=1",
                        (fid,),
                    ).fetchall()
                    for r in rows:
                        if r[0] not in visited:
                            visited[r[0]] = d
                            next_frontier.append(r[0])
                if direction in ("dependencies", "both"):
                    rows = self._conn.execute(
                        "SELECT target_file_id FROM edges WHERE source_file_id=? AND active=1",
                        (fid,),
                    ).fetchall()
                    for r in rows:
                        if r[0] not in visited:
                            visited[r[0]] = d
                            next_frontier.append(r[0])
            frontier = next_frontier
            if not frontier:
                break

        nodes = []
        for fid, hop in visited.items():
            row = self._conn.execute(
                """SELECT path, language, blast_score,
                          direct_dependents, transitive_dependents
                   FROM files WHERE id=?""",
                (fid,),
            ).fetchone()
            if row:
                nodes.append({
                    "file":                  row[0],
                    "language":              row[1],
                    "blast_score":           row[2],
                    "direct_dependents":     row[3],
                    "transitive_dependents": row[4],
                    "hop":                   hop,
                })

        return {
            "center":    file_path,
            "direction": direction,
            "depth":     depth,
            "nodes":     sorted(nodes, key=lambda x: (x["hop"], x["file"])),
        }

    # ── status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return a snapshot of store state for `blastradius db status`."""
        try:
            fts_rows = self._conn.execute(
                "SELECT COUNT(*) FROM symbols_fts"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            fts_rows = 0
        return {
            "schema_version":      self.get_meta("schema_version") or "unknown",
            "last_indexed_commit": self.get_meta("last_indexed_commit") or "none",
            "repo_root":           self.get_meta("repo_root") or "unknown",
            "active_files":        self._conn.execute(
                "SELECT COUNT(*) FROM files WHERE active=1"
            ).fetchone()[0],
            "active_edges":        self._conn.execute(
                "SELECT COUNT(*) FROM edges WHERE active=1"
            ).fetchone()[0],
            "active_symbols":      self._conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE active=1"
            ).fetchone()[0],
            "fts_symbols":         fts_rows,
            "embedding_model":     self.get_meta("embedding_model") or "none",
            "embedding_dims":      self.get_meta("embedding_dims") or "none",
            "vec_symbols":         "enabled" if self._has_vec else "disabled",
        }

    # ── close ─────────────────────────────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()

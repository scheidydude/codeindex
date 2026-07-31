# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
"""Stdio MCP server — exposes blastradius tools to Claude and other MCP clients."""
from __future__ import annotations
import json
import sys
from pathlib import Path

from blastradius.index import build, load, find_index, find_db, INDEX_FILENAME
from blastradius.index import git_reachable, git_resolve, git_modified
from blastradius.impact import compute_blast_radius
from blastradius.reporter import format_markdown
from blastradius.symbols import SYMBOL_INDEX_FILENAME

TOOLS = [
    {
        "name": "analyze_repo",
        "description": "Analyze a repository and build/refresh its blastradius.json dependency index.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the repo root.",
                }
            },
            "required": ["repo_path"],
        },
    },
    {
        "name": "get_impact",
        "description": (
            "Return the blast-radius impact report for a specific file. "
            "Shows direct dependents, transitive dependents, blast score, and risk level. "
            "Call this before modifying any file to understand change impact."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to assess (relative to repo root or absolute).",
                },
                "index_path": {
                    "type": "string",
                    "description": "Path to blastradius.json. Auto-discovered if omitted.",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "get_dependencies",
        "description": "Return the direct imports and imported-by list for a specific file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file (relative to repo root or absolute).",
                },
                "index_path": {
                    "type": "string",
                    "description": "Path to blastradius.json. Auto-discovered if omitted.",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "get_high_blast_files",
        "description": "Return all files whose blast score exceeds a threshold, sorted by score descending.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "threshold": {
                    "type": "number",
                    "description": "Minimum blast score to include. Default: 5.",
                },
                "index_path": {
                    "type": "string",
                    "description": "Path to blastradius.json. Auto-discovered if omitted.",
                },
            },
        },
    },
    {
        "name": "lookup_symbol",
        "description": (
            "Find where a function, class, struct, or other symbol is defined. "
            "Returns file path and line number via O(1) index lookup — no file scanning. "
            "Requires symbolindex.json (run build_symbol_index first)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact symbol name to look up.",
                },
                "symbol_index_path": {
                    "type": "string",
                    "description": "Path to symbolindex.json. Auto-discovered if omitted.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "build_symbol_index",
        "description": (
            "Build or refresh the symbol index (symbolindex.json) for a repository. "
            "Extracts every function, class, struct, and type with file and line number. "
            "Run once after cloning or after major refactors, then use lookup_symbol."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the repo root.",
                },
            },
            "required": ["repo_path"],
        },
    },
    {
        "name": "semantic_search",
        "description": (
            "Hybrid semantic + keyword + graph search over indexed symbols. "
            "Fuses semantic KNN (if sqlite-vec + embedding endpoint configured), FTS5 keyword "
            "matching, and structural graph expansion via Reciprocal Rank Fusion. "
            "Finds relevant functions/classes/symbols without knowing their exact names. "
            "Degrades gracefully to keyword + graph when embeddings are unavailable."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language or keyword query, e.g. 'validate auth token'.",
                },
                "k": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Default: 10.",
                },
                "as_of": {
                    "type": "string",
                    "description": "Optional commit/ref — restrict to symbols visible at that point in history.",
                },
                "db_path": {
                    "type": "string",
                    "description": "Path to .blastradius/index.db. Auto-discovered from cwd if omitted.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "temporal_impact",
        "description": (
            "Compute blast-radius impact for a file at a historical commit/ref. "
            "Shows which files depended on it at that point in time, not just at HEAD. "
            "Requires blastradius analyze to have been run at (or near) the target commit, "
            "or blastradius history to have backfilled temporal data."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Repo-relative file path, e.g. 'src/auth.py'.",
                },
                "as_of": {
                    "type": "string",
                    "description": "Commit hash, branch, or tag to evaluate impact at.",
                },
                "db_path": {
                    "type": "string",
                    "description": "Path to .blastradius/index.db. Auto-discovered from cwd if omitted.",
                },
            },
            "required": ["file"],
        },
    },
    {
        "name": "graph_query",
        "description": (
            "Return the k-hop dependency neighborhood of a file. "
            "Use direction='dependents' to find what would break if this file changed, "
            "'dependencies' to see what this file relies on, or 'both' for the full neighborhood."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Repo-relative file path, e.g. 'src/auth.py'.",
                },
                "direction": {
                    "type": "string",
                    "enum": ["dependents", "dependencies", "both"],
                    "description": "Traversal direction. Default: 'both'.",
                },
                "depth": {
                    "type": "integer",
                    "description": "Number of hops to traverse. Default: 2.",
                },
                "db_path": {
                    "type": "string",
                    "description": "Path to .blastradius/index.db. Auto-discovered from cwd if omitted.",
                },
            },
            "required": ["file"],
        },
    },
    {
        "name": "changed_since",
        "description": (
            "List files and edges added or removed since a commit/ref. "
            "Useful for understanding what has changed between two points in history — "
            "new modules introduced, dependencies removed, structural drift."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Commit hash, branch, or tag to compare against current HEAD.",
                },
                "db_path": {
                    "type": "string",
                    "description": "Path to .blastradius/index.db. Auto-discovered from cwd if omitted.",
                },
            },
            "required": ["ref"],
        },
    },
]


def _resolve_index(index_path: str | None) -> dict:
    if index_path:
        return load(Path(index_path))
    discovered = find_index(Path.cwd())
    if not discovered:
        raise FileNotFoundError(
            f"No {INDEX_FILENAME} found. Run: blastradius analyze <repo>"
        )
    return load(discovered)


def _resolve_file_id(file_path: str, data: dict) -> str | None:
    fp = Path(file_path)
    node_ids = {n["id"] for n in data["nodes"]}
    if str(fp) in node_ids:
        return str(fp)
    # Try matching by suffix (relative path without leading ./)
    clean = str(fp).lstrip("./")
    for nid in node_ids:
        if nid.endswith(clean) or clean.endswith(nid):
            return nid
    return None


def _call_analyze_repo(params: dict) -> dict:
    repo_path = params["repo_path"]
    data = build(repo_path)
    return {
        "success": True,
        "files":   data["meta"]["total_files"],
        "loc":     data["meta"]["total_loc"],
        "languages": data["meta"].get("languages", []),
    }


def _call_get_impact(params: dict) -> dict:
    data = _resolve_index(params.get("index_path"))
    file_id = _resolve_file_id(params["file_path"], data)
    if not file_id:
        return {"error": f"File not found in index: {params['file_path']}"}

    blast_map = compute_blast_radius(data["nodes"], data["links"])
    blast = blast_map.get(file_id)
    if not blast:
        return {"error": f"No blast data for {file_id}"}

    total = len([n for n in data["nodes"] if not n.get("type") == "import"])
    report = format_markdown(file_id, blast, total)
    return {"file": file_id, "report": report, "blast_score": blast["blast_score"]}


def _call_get_dependencies(params: dict) -> dict:
    data = _resolve_index(params.get("index_path"))
    file_id = _resolve_file_id(params["file_path"], data)
    if not file_id:
        return {"error": f"File not found in index: {params['file_path']}"}

    node = next((n for n in data["nodes"] if n["id"] == file_id), None)
    if not node:
        return {"error": f"Node not found: {file_id}"}

    return {
        "file":        file_id,
        "imports":     node.get("imports", []),
        "imported_by": node.get("imported_by", []),
        "blast_score": node.get("blast_score", 0),
    }


def _call_get_high_blast_files(params: dict) -> dict:
    data = _resolve_index(params.get("index_path"))
    threshold = float(params.get("threshold", 5))
    _NON_FILE_TYPES = {"import", "service", "pipeline", "database"}
    results = [
        {
            "file":       n["id"],
            "blast_score": n.get("blast_score", 0),
            "loc":        n.get("loc", 0),
            "direct":     n.get("direct_dependents", 0),
            "transitive": n.get("transitive_dependents", 0),
        }
        for n in data["nodes"]
        if n.get("blast_score", 0) >= threshold and n.get("type") not in _NON_FILE_TYPES
    ]
    results.sort(key=lambda x: x["blast_score"], reverse=True)
    return {"files": results, "count": len(results), "threshold": threshold}


def _find_symbol_index(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        p = d / SYMBOL_INDEX_FILENAME
        if p.exists():
            return p
    return None


def _resolve_symbol_index(symbol_index_path: str | None) -> dict:
    if symbol_index_path:
        p = Path(symbol_index_path)
    else:
        p = _find_symbol_index(Path.cwd())
    if not p or not p.exists():
        raise FileNotFoundError(
            f"No {SYMBOL_INDEX_FILENAME} found. Run: blastradius symbols <repo>"
        )
    return json.loads(p.read_text())


def _call_lookup_symbol(params: dict) -> dict:
    from blastradius.store import Store

    name = params["name"]
    matches = []

    # Prefer SQLite DB (same data source as semantic_search)
    db_path = find_db(Path.cwd())
    if db_path:
        store = Store(db_path)
        rows = store.lookup_by_name(name)
        store.close()
        matches = [
            {
                "file":     r["file"],
                "line":     r["line"],
                "kind":     r["kind"],
                "exported": r["exported"],
                "methods":  [],
            }
            for r in rows
        ]

    # Fall back to symbolindex.json when DB not available
    if not matches:
        try:
            sym_data = _resolve_symbol_index(params.get("symbol_index_path"))
            raw = sym_data.get("symbols", {}).get(name, [])
            matches = [
                {
                    "file":     m["file"],
                    "line":     m["line"],
                    "kind":     m.get("kind", "?"),
                    "exported": m.get("exported", True),
                    "methods":  m.get("methods", []),
                }
                for m in raw
            ]
        except FileNotFoundError:
            pass

    if not matches:
        return {"found": False, "name": name, "matches": []}
    return {"found": True, "name": name, "matches": matches}


def _call_build_symbol_index(params: dict) -> dict:
    from blastradius.symbols import build_symbol_index as _build, write_standalone  # noqa: PLC0415
    repo_path = params["repo_path"]
    symbol_data = _build(repo_path)
    out = Path(repo_path) / SYMBOL_INDEX_FILENAME
    write_standalone(symbol_data, out)
    return {
        "success":       True,
        "total_symbols": symbol_data["meta"]["total_symbols"],
        "files":         len(symbol_data["file_symbols"]),
        "output":        str(out),
    }


def _resolve_db(params: dict):
    """Return an open Store for the db_path in params or auto-discovered from cwd."""
    from blastradius.store import Store
    db_path_str = params.get("db_path")
    if db_path_str:
        db_path = Path(db_path_str)
    else:
        db_path = find_db(Path.cwd())
    if not db_path or not db_path.exists():
        raise FileNotFoundError(
            "No .blastradius/index.db found — run: blastradius analyze <repo>"
        )
    return Store(db_path)


def _call_semantic_search(params: dict) -> dict:
    from blastradius.semantic.search import hybrid_search
    import os

    store = _resolve_db(params)

    provider = None
    endpoint = os.environ.get("BLASTRADIUS_EMBEDDING_ENDPOINT", "")
    model = os.environ.get("BLASTRADIUS_EMBEDDING_MODEL", "")
    dims_str = os.environ.get("BLASTRADIUS_EMBEDDING_DIMS", "")
    if endpoint and model and dims_str:
        try:
            from blastradius.semantic.provider import OpenAIEmbeddingProvider
            provider = OpenAIEmbeddingProvider(
                endpoint=endpoint, model=model, dims=int(dims_str)
            )
        except Exception:
            pass

    as_of_reachable = None
    as_of = params.get("as_of")
    if as_of:
        repo_root_str = store.get_meta("repo_root")
        repo_root = Path(repo_root_str) if repo_root_str else Path.cwd()
        full_hash = git_resolve(repo_root, as_of)
        if full_hash:
            as_of_reachable = git_reachable(repo_root, full_hash)

    results = hybrid_search(
        store=store,
        query=params["query"],
        k=int(params.get("k", 10)),
        as_of_reachable=as_of_reachable,
        provider=provider,
    )
    store.close()

    # File-level aggregation: group by file, sorted by symbol hit count
    from collections import Counter
    file_counts = Counter(r["file"] for r in results)
    files = [{"file": f, "symbol_hits": c} for f, c in file_counts.most_common()]
    return {"query": params["query"], "count": len(results), "files": files, "results": results}


def _call_temporal_impact(params: dict) -> dict:
    store = _resolve_db(params)
    repo_root_str = store.get_meta("repo_root")
    repo_root = Path(repo_root_str) if repo_root_str else Path.cwd()

    as_of = params.get("as_of")
    file_arg = params["file"]

    # Resolve file path against indexed files
    all_paths = [
        r[0] for r in store._conn.execute("SELECT path FROM files").fetchall()
    ]
    clean = file_arg.lstrip("./")
    file_id = None
    if file_arg in all_paths:
        file_id = file_arg
    else:
        for p in all_paths:
            if p.endswith(clean) or clean.endswith(p):
                file_id = p
                break

    if not file_id:
        store.close()
        return {"error": f"File not found in index: {file_arg}"}

    if as_of:
        full_hash = git_resolve(repo_root, as_of)
        if not full_hash:
            store.close()
            return {"error": f"Could not resolve ref: {as_of}"}
        reachable = git_reachable(repo_root, full_hash)
        blast = store.as_of_impact(file_id, reachable)
        store.close()
        if blast is None:
            return {
                "error": (
                    f"No temporal data for {file_id} at {as_of}. "
                    "Run `blastradius history` to backfill or `blastradius analyze` at each commit."
                )
            }
        return {
            "file":                  file_id,
            "as_of":                 as_of,
            "blast_score":           blast["blast_score"],
            "direct_dependents":     blast["direct_dependents"],
            "transitive_dependents": blast["transitive_dependents"],
            "direct_ids":            blast["direct_ids"],
            "transitive_ids":        blast["transitive_ids"],
        }

    # Current HEAD: fall back to JSON-based path
    store.close()
    data = _resolve_index(None)
    fid2 = _resolve_file_id(file_arg, data)
    if not fid2:
        return {"error": f"File not found in index: {file_arg}"}
    blast_map = compute_blast_radius(data["nodes"], data["links"])
    blast = blast_map.get(fid2)
    if not blast:
        return {"error": f"No blast data for {fid2}"}
    total = len([n for n in data["nodes"] if n.get("type") != "import"])
    report = format_markdown(fid2, blast, total)
    return {
        "file":                  fid2,
        "blast_score":           blast["blast_score"],
        "direct_dependents":     blast["direct_dependents"],
        "transitive_dependents": blast["transitive_dependents"],
        "direct_ids":            blast["direct_ids"],
        "transitive_ids":        blast["transitive_ids"],
        "report":                report,
    }


def _call_graph_query(params: dict) -> dict:
    store = _resolve_db(params)
    file_arg = params["file"]
    direction = params.get("direction", "both")
    depth = int(params.get("depth", 2))

    # Resolve path against indexed files
    all_paths = [
        r[0] for r in store._conn.execute(
            "SELECT path FROM files WHERE active=1"
        ).fetchall()
    ]
    clean = file_arg.lstrip("./")
    file_id = None
    if file_arg in all_paths:
        file_id = file_arg
    else:
        for p in all_paths:
            if p.endswith(clean) or clean.endswith(p):
                file_id = p
                break

    if not file_id:
        store.close()
        return {"error": f"File not found in active index: {file_arg}"}

    result = store.neighborhood(file_id, direction, depth)
    store.close()
    return result


def _call_changed_since(params: dict) -> dict:
    store = _resolve_db(params)
    repo_root_str = store.get_meta("repo_root")
    repo_root = Path(repo_root_str) if repo_root_str else Path.cwd()

    ref = params["ref"]
    full_hash = git_resolve(repo_root, ref)
    if not full_hash:
        store.close()
        return {"error": f"Could not resolve ref: {ref}"}

    reachable = git_reachable(repo_root, full_hash)
    if not reachable:
        store.close()
        return {"error": f"No commits reachable from {ref}"}

    result = store.changed_since(reachable)
    last_indexed = store.get_meta("last_indexed_commit") or ""
    store.close()
    result["ref"] = ref

    # Add content-modified files from git (files changed but not added/removed structurally)
    modified = git_modified(repo_root, full_hash)
    added_set = set(result.get("added_files", []))
    removed_set = set(result.get("removed_files", []))
    result["modified_files"] = [f for f in modified if f not in added_set and f not in removed_set]

    # Filter edges to those touching the changed file set — whole-graph edge noise otherwise.
    touched = added_set | removed_set | set(result["modified_files"])
    all_ae = result["added_edges"]
    all_re = result["removed_edges"]
    ae_filtered = [e for e in all_ae if e["source"] in touched or e["target"] in touched]
    re_filtered = [e for e in all_re if e["source"] in touched or e["target"] in touched]
    result["added_edges"]   = ae_filtered
    result["removed_edges"] = re_filtered
    suppressed = (len(all_ae) - len(ae_filtered)) + (len(all_re) - len(re_filtered))
    if suppressed:
        result["suppressed_edge_count"] = suppressed

    analyze_origin_count = sum(
        1 for e in ae_filtered
        if last_indexed and e.get("first_seen_commit") == last_indexed
    )
    if analyze_origin_count:
        result["analyze_origin_edge_count"] = analyze_origin_count
        # bootstrap_gap=True means history has been run but these edges still can't be
        # dated further — they predate the first analyze. False means history was never run.
        result["bootstrap_gap"] = not bool(result.get("warning"))

    return result


_HANDLERS = {
    "analyze_repo":        _call_analyze_repo,
    "get_impact":          _call_get_impact,
    "get_dependencies":    _call_get_dependencies,
    "get_high_blast_files": _call_get_high_blast_files,
    "lookup_symbol":       _call_lookup_symbol,
    "build_symbol_index":  _call_build_symbol_index,
    "semantic_search":     _call_semantic_search,
    "temporal_impact":     _call_temporal_impact,
    "graph_query":         _call_graph_query,
    "changed_since":       _call_changed_since,
}


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _handle(msg: dict) -> dict | None:
    method  = msg.get("method", "")
    req_id  = msg.get("id")
    params  = msg.get("params", {})

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "blastradius", "version": "0.1.0"},
        })

    if method == "notifications/initialized":
        return None  # no response for notifications

    if method == "tools/list":
        return ok({"tools": TOOLS})

    if method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})
        handler = _HANDLERS.get(tool_name)
        if not handler:
            return err(-32601, f"Unknown tool: {tool_name}")
        try:
            result = handler(tool_args)
            return ok({
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
            })
        except Exception as e:
            return ok({
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            })

    if method == "ping":
        return ok({})

    return err(-32601, f"Method not found: {method}")


def serve() -> None:
    print("[blastradius MCP] ready on stdio", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "Parse error"}})
            continue
        response = _handle(msg)
        if response is not None:
            _send(response)

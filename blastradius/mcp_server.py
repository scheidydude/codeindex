# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

"""Stdio MCP server — exposes blastradius tools to MCP clients."""

import json
import sys
from importlib.metadata import version
from pathlib import Path

import anyio
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage

from blastradius.artifacts import Artifacts, resolve_file_id
from blastradius.impact import compute_blast_radius
from blastradius.index import (
    INDEX_FILENAME,
    build,
    git_modified,
    git_reachable,
    git_resolve,
)
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
                    "minimum": 1,
                    "description": "Positive maximum number of results to return. Default: 10.",
                },
                "as_of": {
                    "type": "string",
                    "description": "Optional commit/ref — restrict to symbols visible at that point in history.",
                },
                "db_path": {
                    "type": "string",
                    "description": "Path to .blastradius/index.db. Defaults to --repo or cwd discovery.",
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
                    "description": "Path to .blastradius/index.db. Defaults to --repo or cwd discovery.",
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
                    "minimum": 0,
                    "description": "Nonnegative number of hops to traverse. Default: 2.",
                },
                "db_path": {
                    "type": "string",
                    "description": "Path to .blastradius/index.db. Defaults to --repo or cwd discovery.",
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
                    "description": "Path to .blastradius/index.db. Defaults to --repo or cwd discovery.",
                },
            },
            "required": ["ref"],
        },
    },
]


def _call_analyze_repo(params: dict, artifacts: Artifacts) -> dict:
    data = build(str(artifacts.path(params["repo_path"])))
    return {
        "success": True,
        "files": data["meta"]["total_files"],
        "loc": data["meta"]["total_loc"],
        "languages": data["meta"].get("languages", []),
    }


def _call_get_impact(params: dict, artifacts: Artifacts) -> dict:
    data, file_id = artifacts.index_for_file(
        params["file_path"], params.get("index_path")
    )

    blast_map = compute_blast_radius(data["nodes"], data["links"])
    blast = blast_map.get(file_id)
    if not blast:
        raise ValueError(f"No blast data for {file_id}")

    total = len([n for n in data["nodes"] if n.get("type") != "import"])
    report = format_markdown(file_id, blast, total)
    return {"file": file_id, "report": report, "blast_score": blast["blast_score"]}


def _call_get_dependencies(params: dict, artifacts: Artifacts) -> dict:
    data, file_id = artifacts.index_for_file(
        params["file_path"], params.get("index_path")
    )

    node = next((n for n in data["nodes"] if n["id"] == file_id), None)
    if not node:
        raise ValueError(f"Node not found: {file_id}")

    return {
        "file": file_id,
        "imports": node.get("imports", []),
        "imported_by": node.get("imported_by", []),
        "blast_score": node.get("blast_score", 0),
    }


def _call_get_high_blast_files(params: dict, artifacts: Artifacts) -> dict:
    data = artifacts.index(params.get("index_path"))
    threshold = float(params.get("threshold", 5))
    _NON_FILE_TYPES = {"import", "service", "pipeline", "database"}
    results = [
        {
            "file": n["id"],
            "blast_score": n.get("blast_score", 0),
            "loc": n.get("loc", 0),
            "direct": n.get("direct_dependents", 0),
            "transitive": n.get("transitive_dependents", 0),
        }
        for n in data["nodes"]
        if n.get("blast_score", 0) >= threshold and n.get("type") not in _NON_FILE_TYPES
    ]
    results.sort(key=lambda x: x["blast_score"], reverse=True)
    return {"files": results, "count": len(results), "threshold": threshold}


def _call_lookup_symbol(params: dict, artifacts: Artifacts) -> dict:
    name = params["name"]
    matches = []
    explicit = params.get("symbol_index_path")

    # Prefer SQLite DB (same data source as semantic_search)
    try:
        db_path = artifacts.find(".blastradius/index.db")
    except FileNotFoundError:
        db_path = None
    if db_path and explicit is None:
        store = artifacts.database(db_path)
        rows = store.lookup_by_name(name)
        matches = [
            {
                "file": r["file"],
                "line": r["line"],
                "kind": r["kind"],
                "exported": r["exported"],
                "methods": [],
            }
            for r in rows
        ]

    # Fall back to symbolindex.json when DB not available
    if not matches:
        try:
            sym_data = artifacts.symbol_index(explicit)
            raw = sym_data.get("symbols", {}).get(name, [])
            matches = [
                {
                    "file": m["file"],
                    "line": m["line"],
                    "kind": m.get("kind", "?"),
                    "exported": m.get("exported", True),
                    "methods": m.get("methods", []),
                }
                for m in raw
            ]
        except FileNotFoundError:
            if explicit is not None or db_path is None:
                raise

    if not matches:
        return {"found": False, "name": name, "matches": []}
    return {"found": True, "name": name, "matches": matches}


def _call_build_symbol_index(params: dict, artifacts: Artifacts) -> dict:
    from blastradius.symbols import build_symbol_index as _build
    from blastradius.symbols import write_standalone

    repo_path = artifacts.path(params["repo_path"])
    symbol_data = _build(str(repo_path))
    out = Path(repo_path) / SYMBOL_INDEX_FILENAME
    write_standalone(symbol_data, out)
    return {
        "success": True,
        "total_symbols": symbol_data["meta"]["total_symbols"],
        "files": len(symbol_data["file_symbols"]),
        "output": str(out),
    }


def _call_semantic_search(params: dict, artifacts: Artifacts) -> dict:
    import os

    from blastradius.semantic.search import hybrid_search

    store = artifacts.database(params.get("db_path"))

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
        except Exception:  # noqa: BLE001, S110
            pass

    as_of_reachable = None
    as_of = params.get("as_of")
    if as_of:
        repo_root = artifacts.repository(store, params.get("db_path"))
        full_hash = git_resolve(repo_root, as_of)
        if not full_hash:
            raise ValueError(f"Could not resolve ref: {as_of}")
        as_of_reachable = git_reachable(repo_root, full_hash)

    results = hybrid_search(
        store=store,
        query=params["query"],
        k=int(params.get("k", 10)),
        as_of_reachable=as_of_reachable,
        provider=provider,
    )
    # File-level aggregation: group by file, sorted by symbol hit count
    from collections import Counter

    file_counts = Counter(r["file"] for r in results)
    files = [{"file": f, "symbol_hits": c} for f, c in file_counts.most_common()]
    return {
        "query": params["query"],
        "count": len(results),
        "files": files,
        "results": results,
    }


def _call_temporal_impact(params: dict, artifacts: Artifacts) -> dict:
    store = artifacts.database(params.get("db_path"))
    repo_root = artifacts.repository(store, params.get("db_path"))

    as_of = params.get("as_of")
    file_arg = params["file"]

    # Resolve file path against indexed files
    all_paths = [r[0] for r in store._conn.execute("SELECT path FROM files").fetchall()]
    file_id = resolve_file_id(file_arg, all_paths, repo_root)

    if not file_id:
        raise ValueError(f"File not found in index: {file_arg}")

    if as_of:
        full_hash = git_resolve(repo_root, as_of)
        if not full_hash:
            raise ValueError(f"Could not resolve ref: {as_of}")
        reachable = git_reachable(repo_root, full_hash)
        blast = store.as_of_impact(file_id, reachable)
        if blast is None:
            raise ValueError(
                f"No temporal data for {file_id} at {as_of}. "
                "Run `blastradius history` to backfill or `blastradius analyze` at each commit."
            )
        return {
            "file": file_id,
            "as_of": as_of,
            "blast_score": blast["blast_score"],
            "direct_dependents": blast["direct_dependents"],
            "transitive_dependents": blast["transitive_dependents"],
            "direct_ids": blast["direct_ids"],
            "transitive_ids": blast["transitive_ids"],
        }

    # Current HEAD: fall back to JSON-based path
    data = artifacts.index(repo_root / INDEX_FILENAME)
    fid2 = resolve_file_id(file_arg, (n["id"] for n in data["nodes"]), repo_root)
    if not fid2:
        raise ValueError(f"File not found in index: {file_arg}")
    blast_map = compute_blast_radius(data["nodes"], data["links"])
    blast = blast_map.get(fid2)
    if not blast:
        raise ValueError(f"No blast data for {fid2}")
    total = len([n for n in data["nodes"] if n.get("type") != "import"])
    report = format_markdown(fid2, blast, total)
    return {
        "file": fid2,
        "blast_score": blast["blast_score"],
        "direct_dependents": blast["direct_dependents"],
        "transitive_dependents": blast["transitive_dependents"],
        "direct_ids": blast["direct_ids"],
        "transitive_ids": blast["transitive_ids"],
        "report": report,
    }


def _call_graph_query(params: dict, artifacts: Artifacts) -> dict:
    store = artifacts.database(params.get("db_path"))
    file_arg = params["file"]
    direction = params.get("direction", "both")
    depth = int(params.get("depth", 2))

    # Resolve path against indexed files
    all_paths = [
        r[0]
        for r in store._conn.execute("SELECT path FROM files WHERE active=1").fetchall()
    ]
    root = artifacts.repository(store, params.get("db_path"))
    file_id = resolve_file_id(file_arg, all_paths, root)

    if not file_id:
        raise ValueError(f"File not found in active index: {file_arg}")

    result = store.neighborhood(file_id, direction, depth)
    return result


def _call_changed_since(params: dict, artifacts: Artifacts) -> dict:
    store = artifacts.database(params.get("db_path"))
    repo_root = artifacts.repository(store, params.get("db_path"))

    ref = params["ref"]
    full_hash = git_resolve(repo_root, ref)
    if not full_hash:
        raise ValueError(f"Could not resolve ref: {ref}")

    reachable = git_reachable(repo_root, full_hash)
    if not reachable:
        raise ValueError(f"No commits reachable from {ref}")

    result = store.changed_since(reachable)
    last_indexed = store.get_meta("last_indexed_commit") or ""
    result["ref"] = ref

    # Add content-modified files from git (files changed but not added/removed structurally)
    modified = git_modified(repo_root, full_hash)
    added_set = set(result.get("added_files", []))
    removed_set = set(result.get("removed_files", []))
    result["modified_files"] = [
        f for f in modified if f not in added_set and f not in removed_set
    ]

    # Filter edges to those touching the changed file set — whole-graph edge noise otherwise.
    touched = added_set | removed_set | set(result["modified_files"])
    all_ae = result["added_edges"]
    all_re = result["removed_edges"]
    ae_filtered = [
        e for e in all_ae if e["source"] in touched or e["target"] in touched
    ]
    re_filtered = [
        e for e in all_re if e["source"] in touched or e["target"] in touched
    ]
    result["added_edges"] = ae_filtered
    result["removed_edges"] = re_filtered
    suppressed = (len(all_ae) - len(ae_filtered)) + (len(all_re) - len(re_filtered))
    if suppressed:
        result["suppressed_edge_count"] = suppressed

    analyze_origin_count = sum(
        1
        for e in ae_filtered
        if last_indexed and e.get("first_seen_commit") == last_indexed
    )
    if analyze_origin_count:
        result["analyze_origin_edge_count"] = analyze_origin_count
        # bootstrap_gap=True means history has been run but these edges still can't be
        # dated further — they predate the first analyze. False means history was never run.
        result["bootstrap_gap"] = not bool(result.get("warning"))

    return result


_HANDLERS = {
    "analyze_repo": _call_analyze_repo,
    "get_impact": _call_get_impact,
    "get_dependencies": _call_get_dependencies,
    "get_high_blast_files": _call_get_high_blast_files,
    "lookup_symbol": _call_lookup_symbol,
    "build_symbol_index": _call_build_symbol_index,
    "semantic_search": _call_semantic_search,
    "temporal_impact": _call_temporal_impact,
    "graph_query": _call_graph_query,
    "changed_since": _call_changed_since,
}


async def _validated_stream(source, destination, output):
    """Translate SDK framing errors; valid messages stay entirely SDK-owned."""
    async with source, destination:
        async for message in source:
            if not isinstance(message, Exception):
                await destination.send(message)
                continue
            # SDK stdio emits validation exceptions instead of wire responses.
            details = message.errors() if hasattr(message, "errors") else []
            parse_error = any(e["type"] == "json_invalid" for e in details)
            request = next(
                (e.get("input") for e in details if isinstance(e.get("input"), dict)),
                {},
            )
            if (
                request.get("jsonrpc") == "2.0"
                and isinstance(request.get("method"), str)
                and "id" not in request
            ):
                continue
            request_id = request.get("id")
            if type(request_id) not in (int, str):
                request_id = None
            await output.send(
                SessionMessage(
                    types.JSONRPCError(
                        jsonrpc="2.0",
                        id=request_id,
                        error=types.ErrorData(
                            code=-32700 if parse_error else -32600,
                            message="Parse error" if parse_error else "Invalid Request",
                        ),
                    )
                )
            )


async def _serve(base: Path, discover: bool) -> None:
    limiter = anyio.CapacityLimiter(1)
    validators = {
        tool["name"]: Draft202012Validator(tool["inputSchema"]) for tool in TOOLS
    }

    def execute(handler, arguments):
        with Artifacts(base, discover=discover) as artifacts:
            return handler(arguments, artifacts)

    async def list_tools(ctx, params):
        return types.ListToolsResult(
            tools=[types.Tool.model_validate(tool) for tool in TOOLS]
        )

    async def call_tool(ctx, params):
        if params.arguments is None and "arguments" in params.model_fields_set:
            raise MCPError(-32602, "arguments must be an object when provided")
        handler = _HANDLERS.get(params.name)
        if handler is None:
            raise MCPError(-32602, f"Unknown tool: {params.name}")
        error = next(validators[params.name].iter_errors(params.arguments or {}), None)
        if error:
            field = ".".join(map(str, error.path)) or "arguments"
            raise MCPError(-32602, f"Invalid {field}: {error.message}")
        try:
            result = await anyio.to_thread.run_sync(
                execute, handler, params.arguments or {}, limiter=limiter
            )
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text", text=json.dumps(result, indent=2, allow_nan=False)
                    )
                ]
            )
        except Exception as exc:  # noqa: BLE001 — tool failures must not kill the transport
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Error: {exc}")],
                is_error=True,
            )

    server = Server(
        "blastradius",
        version=version("blastradius-cli"),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    ready = anyio.Event()

    def reject_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    async def checked_stdin():
        # The SDK union currently interprets invalid IDs as notifications,
        # discarding the ID. Reject them before that information is lost.
        async for line in anyio.wrap_file(sys.stdin):
            try:
                request = json.loads(line, parse_constant=reject_constant)
            except ValueError:
                await ready.wait()
                await output.send(
                    SessionMessage(
                        types.JSONRPCError(
                            jsonrpc="2.0",
                            id=None,
                            error=types.ErrorData(code=-32700, message="Parse error"),
                        )
                    )
                )
                continue
            if (
                isinstance(request, dict)
                and "method" in request
                and "id" in request
                and type(request["id"]) not in (int, str)
            ):
                await ready.wait()
                await output.send(
                    SessionMessage(
                        types.JSONRPCError(
                            jsonrpc="2.0",
                            id=None,
                            error=types.ErrorData(
                                code=-32600,
                                message="Invalid request ID: expected a string or integer",
                            ),
                        )
                    )
                )
                continue
            yield line

    sys.stdin.reconfigure(encoding="utf-8")
    async with stdio_server(stdin=checked_stdin()) as (source, output):
        ready.set()
        destination, incoming = anyio.create_memory_object_stream(0)
        async with anyio.create_task_group() as group:
            group.start_soon(_validated_stream, source, destination, output)
            async with incoming:
                await server.run(
                    incoming, output, server.create_initialization_options()
                )


def serve(repo_path: str | None = None) -> None:
    base = Path(repo_path).resolve() if repo_path is not None else Path.cwd()
    if not base.is_dir():
        raise ValueError(f"Repository directory does not exist: {base}")
    print("[blastradius MCP] ready on stdio", file=sys.stderr)
    anyio.run(_serve, base, repo_path is None)

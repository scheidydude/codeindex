# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_analyze(args: argparse.Namespace) -> None:
    from blastradius.index import build

    repo = args.repo
    output = Path(args.output) if args.output else None

    if args.watch:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            print(
                "watchdog not installed — run: "
                "uv tool install --force 'blastradius-cli[watch]'",
                file=sys.stderr,
            )
            sys.exit(1)

        import threading

        WATCHED_EXTS = {
            ".py",
            ".js",
            ".ts",
            ".jsx",
            ".tsx",
            ".mjs",
            ".cjs",
            ".rb",
            ".go",
            ".rs",
            ".java",
            ".kt",
            ".php",
            ".yml",
            ".yaml",
            ".sql",
            ".prisma",
        }
        dest = output or (Path(repo).resolve() / "blastradius.json")

        class _Watcher(FileSystemEventHandler):
            def __init__(self):
                self._timer = None

            def _rebuild(self):
                print("[watch] change detected, re-indexing…", file=sys.stderr)
                build(repo, dest)

            def on_modified(self, event):
                if event.is_directory:
                    return
                if Path(event.src_path).suffix in WATCHED_EXTS:
                    if self._timer:
                        self._timer.cancel()
                    self._timer = threading.Timer(1.0, self._rebuild)
                    self._timer.start()

        build(repo, dest)
        observer = Observer()
        observer.schedule(_Watcher(), repo, recursive=True)
        observer.start()
        print(f"[watch] watching {repo} — Ctrl+C to stop", file=sys.stderr)
        try:
            while True:
                import time

                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()
    else:
        build(repo, output)


def _cmd_impact(args: argparse.Namespace) -> None:
    from blastradius.impact import compute_blast_radius
    from blastradius.index import (
        INDEX_FILENAME,
        find_db,
        find_index,
        git_reachable,
        git_resolve,
        load,
    )
    from blastradius.reporter import format_markdown, format_stdout

    as_of = getattr(args, "as_of", None)

    if as_of:
        # ── temporal path: query DB for historical blast radius ────────────────
        from blastradius.store import Store

        repo = Path(args.file).resolve().parent
        db_path = find_db(repo) or find_db(Path.cwd())
        if not db_path or not db_path.exists():
            # Try discovering from cwd upward
            db_path = find_db(Path.cwd())
        if not db_path or not db_path.exists():
            print(
                "No .blastradius/index.db found — run: blastradius analyze <repo>",
                file=sys.stderr,
            )
            sys.exit(1)

        # Resolve repo root from DB meta
        store = Store(db_path)
        repo_root_str = store.get_meta("repo_root")
        repo_root = Path(repo_root_str) if repo_root_str else Path.cwd()

        full_hash = git_resolve(repo_root, as_of)
        if not full_hash:
            print(f"Could not resolve ref: {as_of}", file=sys.stderr)
            sys.exit(1)

        reachable = git_reachable(repo_root, full_hash)
        if not reachable:
            print(f"No commits reachable from {as_of}", file=sys.stderr)
            sys.exit(1)

        # Resolve file_id (may be relative or absolute)
        fp = args.file
        clean = fp.lstrip("./")
        all_paths = [
            r[0] for r in store._conn.execute("SELECT path FROM files").fetchall()
        ]
        file_id = None
        if fp in all_paths:
            file_id = fp
        else:
            for p in all_paths:
                if p.endswith(clean) or clean.endswith(p):
                    file_id = p
                    break

        if not file_id:
            print(f"File not found in index: {fp}", file=sys.stderr)
            sys.exit(1)

        blast = store.as_of_impact(file_id, reachable)
        store.close()

        if blast is None:
            print(
                f"No temporal data for {file_id} at {as_of}. "
                "Run `blastradius history` to backfill, or `blastradius analyze` at each commit.",
                file=sys.stderr,
            )
            sys.exit(1)

        total = len(all_paths)
        if args.json:
            print(
                json.dumps(
                    {
                        "file": file_id,
                        "as_of": as_of,
                        "blast_score": blast["blast_score"],
                        "direct_dependents": blast["direct_dependents"],
                        "transitive_dependents": blast["transitive_dependents"],
                        "direct_ids": blast["direct_ids"],
                        "transitive_ids": blast["transitive_ids"],
                    },
                    indent=2,
                )
            )
        else:
            print(f"[as-of {as_of[:12]}]  ", end="")
            print(format_stdout(file_id, blast, total))
        return

    # ── current-HEAD path (unchanged) ─────────────────────────────────────────
    if args.index:
        index_path = Path(args.index)
    else:
        index_path = find_index(Path(args.file).parent)
        if not index_path:
            index_path = find_index(Path.cwd())
        if not index_path:
            print(
                f"No {INDEX_FILENAME} found. Run: blastradius analyze <repo>",
                file=sys.stderr,
            )
            sys.exit(1)

    data = load(index_path)
    node_ids = {n["id"] for n in data["nodes"]}

    fp = args.file
    file_id = None
    if fp in node_ids:
        file_id = fp
    else:
        clean = fp.lstrip("./")
        for nid in node_ids:
            if nid.endswith(clean) or clean.endswith(nid):
                file_id = nid
                break

    if not file_id:
        print(f"File not found in index: {fp}", file=sys.stderr)
        print("Available nodes (first 20):", file=sys.stderr)
        for nid in sorted(node_ids)[:20]:
            print(f"  {nid}", file=sys.stderr)
        sys.exit(1)

    blast_map = compute_blast_radius(data["nodes"], data["links"])
    blast = blast_map[file_id]
    total = len([n for n in data["nodes"] if n.get("type") != "import"])

    if args.out:
        report = format_markdown(file_id, blast, total)
        Path(args.out).write_text(report)
        print(f"Impact report written to {args.out}")
    elif args.json:
        print(
            json.dumps(
                {
                    "file": file_id,
                    "blast_score": blast["blast_score"],
                    "direct_dependents": blast["direct_dependents"],
                    "transitive_dependents": blast["transitive_dependents"],
                    "direct_ids": blast["direct_ids"],
                    "transitive_ids": blast["transitive_ids"],
                },
                indent=2,
            )
        )
    else:
        print(format_stdout(file_id, blast, total))


def _cmd_serve(args: argparse.Namespace) -> None:
    if args.mcp:
        from blastradius.mcp_server import serve

        serve(repo_path=args.repo)
    else:
        from blastradius.viz_server import serve

        output = Path(args.output) if getattr(args, "output", None) else None
        serve(
            repo_path=args.repo or ".",
            port=args.port,
            watch=args.watch,
            output=output,
        )


def _cmd_symbols(args: argparse.Namespace) -> None:
    from blastradius.index import INDEX_FILENAME, find_index
    from blastradius.symbols import (
        SYMBOL_INDEX_FILENAME,
        build_symbol_index,
        write_claude_md,
        write_inline,
        write_standalone,
    )

    repo = Path(args.repo).resolve()
    symbol_data = build_symbol_index(str(repo))

    if args.inline:
        if args.index:
            index_path = Path(args.index)
        else:
            index_path = find_index(repo)
            if not index_path:
                print(
                    f"No {INDEX_FILENAME} found — run: blastradius analyze <repo> first, "
                    "or pass --index <path>",
                    file=sys.stderr,
                )
                sys.exit(1)
        write_inline(symbol_data, index_path)
    else:
        output = Path(args.output) if args.output else (repo / SYMBOL_INDEX_FILENAME)
        write_standalone(symbol_data, output)

    if args.claude_md:
        claude_path = (
            Path(args.claude_md_path) if args.claude_md_path else (repo / "CLAUDE.md")
        )
        write_claude_md(symbol_data, claude_path, exported_only=not args.all_symbols)


def _cmd_lookup(args: argparse.Namespace) -> None:
    from blastradius.index import db_path_for, find_index
    from blastradius.store import Store

    name = args.name
    matches = []

    # Prefer SQLite DB (same data source as `search`)
    index_path = find_index(Path.cwd())
    if index_path:
        db_path = db_path_for(index_path.parent)
        if db_path.exists():
            store = Store(db_path)
            rows = store.lookup_by_name(name)
            store.close()
            matches = [
                {
                    "file": r["file"],
                    "line": r["line"],
                    "kind": r["kind"],
                    "exported": r["exported"],
                }
                for r in rows
            ]

    # Fall back to symbolindex.json when DB not available
    if not matches and not args.index:
        try:
            from blastradius.artifacts import resolve_symbol_index

            sym_data = resolve_symbol_index(None)
            matches = sym_data.get("symbols", {}).get(name, [])
        except FileNotFoundError:
            pass
    elif not matches and args.index:
        from blastradius.artifacts import resolve_symbol_index

        sym_data = resolve_symbol_index(args.index)
        matches = sym_data.get("symbols", {}).get(name, [])

    if not matches:
        print(f"Symbol `{name}` not found in index.", file=sys.stderr)
        print(
            "(Only symbols defined in this repo are indexed; third-party imports are not.)",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.json:
        print(json.dumps({"name": name, "matches": matches}, indent=2))
    else:
        repo_root = index_path.parent if index_path else Path.cwd()
        for m in matches:
            methods = (
                f"  methods: {', '.join(m['methods'])}" if m.get("methods") else ""
            )
            print(f"{m['file']}:{m['line']}  {name}  ({m.get('kind', '?')}){methods}")
            # Print a short snippet around the definition
            src = repo_root / m["file"]
            if src.exists():
                lines = src.read_text(errors="replace").splitlines()
                start = max(0, m["line"] - 1)
                end = min(len(lines), m["line"] + 4)
                print()
                for i, ln in enumerate(lines[start:end], start=start + 1):
                    marker = ">" if i == m["line"] else " "
                    print(f"  {marker} {i:4d} | {ln}")
                print()


def _cmd_dependencies(args: argparse.Namespace) -> None:
    from blastradius.index import INDEX_FILENAME, find_index, load

    if args.index:
        index_path = Path(args.index)
    else:
        index_path = find_index(Path(args.file).parent) or find_index(Path.cwd())
        if not index_path:
            print(
                f"No {INDEX_FILENAME} found. Run: blastradius analyze <repo>",
                file=sys.stderr,
            )
            sys.exit(1)
    data = load(index_path)
    fp = args.file
    clean = fp.lstrip("./")
    node = next(
        (
            n
            for n in data["nodes"]
            if n["id"] == fp or n["id"].endswith(clean) or clean.endswith(n["id"])
        ),
        None,
    )
    if not node:
        print(f"File not found in index: {fp}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(
            json.dumps(
                {
                    "file": node["id"],
                    "imports": node.get("imports", []),
                    "imported_by": node.get("imported_by", []),
                    "blast_score": node.get("blast_score", 0),
                },
                indent=2,
            )
        )
    else:
        print(f"File: {node['id']}  (blast score: {node.get('blast_score', 0):.1f})")
        imports = node.get("imports", [])
        imported_by = node.get("imported_by", [])
        print(f"\nImports ({len(imports)}):")
        for f in imports:
            print(f"  {f}")
        print(f"\nImported by ({len(imported_by)}):")
        for f in imported_by:
            print(f"  {f}")


def _cmd_high_blast(args: argparse.Namespace) -> None:
    from blastradius.index import INDEX_FILENAME, find_index, load

    if args.index:
        index_path = Path(args.index)
    else:
        index_path = find_index(Path.cwd())
        if not index_path:
            print(
                f"No {INDEX_FILENAME} found. Run: blastradius analyze <repo>",
                file=sys.stderr,
            )
            sys.exit(1)
    data = load(index_path)
    threshold = args.threshold
    _NON_FILE_TYPES = {"import", "service", "pipeline", "database"}
    results = sorted(
        [
            n
            for n in data["nodes"]
            if n.get("blast_score", 0) >= threshold
            and n.get("type") not in _NON_FILE_TYPES
        ],
        key=lambda n: n["blast_score"],
        reverse=True,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "threshold": threshold,
                    "count": len(results),
                    "files": [
                        {
                            "file": n["id"],
                            "blast_score": n["blast_score"],
                            "loc": n.get("loc", 0),
                            "direct": n.get("direct_dependents", 0),
                            "transitive": n.get("transitive_dependents", 0),
                        }
                        for n in results
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"Files with blast score >= {threshold}  ({len(results)} found)\n")
        for n in results:
            loc = n.get("loc", 0)
            loc_str = f"  {loc} loc" if loc else ""
            print(
                f"  {n['blast_score']:>6.1f}  {n['id']}"
                f"  ({n.get('direct_dependents', 0)}d / {n.get('transitive_dependents', 0)}t){loc_str}"
            )


def _cmd_symbol_blast(args: argparse.Namespace) -> None:
    """Per-export blast radius: which importers reference each exported symbol."""
    import re as _re

    from blastradius.index import find_db

    db_path = find_db(Path.cwd())
    if not db_path or not db_path.exists():
        print(
            "No .blastradius/index.db found — run: blastradius analyze <repo>",
            file=sys.stderr,
        )
        sys.exit(1)

    from blastradius.store import Store

    store = Store(db_path)
    file_path = args.file

    exports = store.exported_symbols_for_file(file_path)
    if not exports:
        print(f"No exported symbols found for {file_path}", file=sys.stderr)
        print("(Check path is repo-relative and file was analyzed.)", file=sys.stderr)
        store.close()
        sys.exit(1)

    importers = store.importers_of_file(file_path)
    repo_root_str = store.get_meta("repo_root")
    store.close()

    repo_root = Path(repo_root_str) if repo_root_str else Path.cwd()

    # For each importer, read source once and record which exported names appear.
    importer_sources: dict[str, str] = {}
    for imp in importers:
        src_file = repo_root / imp
        if src_file.exists():
            importer_sources[imp] = src_file.read_text(errors="replace")

    # Build symbol → [importer files] map.
    usage: dict[str, list[str]] = {sym["name"]: [] for sym in exports}
    for imp, src in importer_sources.items():
        for sym in exports:
            name = sym["name"]
            # Word-boundary match so "auth" doesn't match "authenticate".
            if _re.search(r"\b" + _re.escape(name) + r"\b", src):
                usage[name].append(imp)

    if args.json:
        print(
            json.dumps(
                {
                    "file": file_path,
                    "exports": [
                        {**sym, "used_by": usage[sym["name"]]} for sym in exports
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"Symbol-level blast radius: {file_path}\n")
        print(f"  {len(exports)} exported symbol(s), {len(importers)} importer(s)\n")
        for sym in exports:
            users = usage[sym["name"]]
            tag = f"  ({sym['kind']}, line {sym['line']})"
            print(f"  {sym['name']}{tag}  →  {len(users)} user(s)")
            for u in sorted(users):
                print(f"    {u}")
        if not importers:
            print("  (no files import this file — safe to change freely)")


def _cmd_db(args: argparse.Namespace) -> None:
    from blastradius.index import find_db
    from blastradius.store import Store

    db_path = Path(args.db) if getattr(args, "db", None) else find_db(Path.cwd())
    if not db_path or not db_path.exists():
        print(
            "No .blastradius/index.db found — run: blastradius analyze <repo>",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.db_command == "status":
        store = Store(db_path)
        info = store.status()
        store.close()
        if getattr(args, "json", False):
            print(json.dumps(info, indent=2))
        else:
            print(f"schema_version      : {info['schema_version']}")
            print(f"repo_root           : {info['repo_root']}")
            print(f"last_indexed_commit : {info['last_indexed_commit']}")
            print(f"active_files        : {info['active_files']}")
            print(f"active_edges        : {info['active_edges']}")
            print(f"active_symbols      : {info['active_symbols']}")
            print(f"fts_symbols         : {info['fts_symbols']}")
    elif args.db_command == "migrate":
        # Schema migrations are applied automatically on Store.__init__.
        # This command is a no-op in Phase 1 but provides the surface for
        # future migration scripts.
        store = Store(db_path)
        current = store.get_meta("schema_version")
        store.close()
        print(f"Schema at version {current} — no pending migrations.")


def _cmd_history(args: argparse.Namespace) -> None:
    from blastradius.index import db_path_for, find_db
    from blastradius.store import Store
    from blastradius.temporal import backfill

    repo = Path(args.repo).resolve()
    db_path = find_db(repo) or db_path_for(repo)
    store = Store(db_path)
    store.set_meta("repo_root", str(repo))
    store._conn.commit()

    max_commits = args.max_commits
    processed, files = backfill(
        root=repo,
        store=store,
        since=args.since,
        max_commits=max_commits,
    )
    store.close()

    if processed == 0:
        print("Nothing to backfill.", file=sys.stderr)
    elif getattr(args, "json", False):
        print(json.dumps({"commits_processed": processed, "files_tracked": files}))


def _cmd_changed_since(args: argparse.Namespace) -> None:
    from blastradius.index import find_db, git_modified, git_reachable, git_resolve
    from blastradius.store import Store

    db_path = Path(args.db) if getattr(args, "db", None) else find_db(Path.cwd())
    if not db_path or not db_path.exists():
        print(
            "No .blastradius/index.db found — run: blastradius analyze <repo>",
            file=sys.stderr,
        )
        sys.exit(1)

    repo = Path(args.repo).resolve() if getattr(args, "repo", None) else Path.cwd()
    ref = args.ref

    full_hash = git_resolve(repo, ref)
    if not full_hash:
        print(f"Could not resolve ref: {ref}", file=sys.stderr)
        sys.exit(1)

    reachable = git_reachable(repo, full_hash)
    if not reachable:
        print(f"No commits reachable from {ref}", file=sys.stderr)
        sys.exit(1)

    store = Store(db_path)
    result = store.changed_since(reachable)
    last_indexed = store.get_meta("last_indexed_commit") or ""
    store.close()

    # Augment with content-modified files from git (files that changed but weren't added/removed)
    modified = git_modified(repo, full_hash)
    added_set = set(result.get("added_files", []))
    removed_set = set(result.get("removed_files", []))
    result["modified_files"] = [
        f for f in modified if f not in added_set and f not in removed_set
    ]

    # Filter edges to only those touching the changed file set — the full graph diff is noise.
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

    # Count added edges whose first_seen_commit matches last analyze HEAD —
    # these may be initial-indexing artifacts rather than genuine new coupling.
    analyze_origin_count = sum(
        1
        for e in ae_filtered
        if last_indexed and e.get("first_seen_commit") == last_indexed
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result.get("warning"):
            print(f"Warning: {result['warning']}", file=sys.stderr)
        mf = result["modified_files"]
        af = result["added_files"]
        rf = result["removed_files"]
        ae = result["added_edges"]
        re_ = result["removed_edges"]
        ref_short = ref[:12] if len(ref) > 12 else ref
        print(f"Changes since {ref_short}:")
        if mf:
            print(f"\n  Modified files ({len(mf)}):")
            for f in mf:
                print(f"    ~ {f}")
        if af:
            print(f"\n  Added files ({len(af)}):")
            for f in af:
                print(f"    + {f}")
        if rf:
            print(f"\n  Removed files ({len(rf)}):")
            for f in rf:
                print(f"    - {f}")
        if ae:
            print(f"\n  Added edges ({len(ae)}):")
            for e in ae:
                print(f"    + {e['source']} → {e['target']}  [{e['kind']}]")
        if re_:
            print(f"\n  Removed edges ({len(re_)}):")
            for e in re_:
                print(f"    - {e['source']} → {e['target']}  [{e['kind']}]")
        if suppressed:
            print(
                f"\n  ({suppressed} unrelated edge changes omitted — run with --json to see all)"
            )
        if analyze_origin_count and ae:
            if result.get("warning"):
                # history has never been run — backfill will resolve these
                print(
                    f"\n  ({analyze_origin_count} of {len(ae)} added edge(s) first seen at last-analyzed HEAD"
                    f" — run `blastradius history` to date them accurately)"
                )
            else:
                # history has been run; these edges predate the first analyze
                print(
                    f"\n  ({analyze_origin_count} of {len(ae)} added edge(s) are bootstrap-gap artifacts:"
                    f" they existed before the first `blastradius analyze` and cannot be dated further)"
                )
        if not any([mf, af, rf, ae, re_]):
            print("  (no changes detected)")


def _cmd_search(args: argparse.Namespace) -> None:
    from blastradius.index import find_db, git_reachable, git_resolve
    from blastradius.semantic.search import hybrid_search
    from blastradius.store import Store

    db_path = Path(args.db) if getattr(args, "db", None) else find_db(Path.cwd())
    if not db_path or not db_path.exists():
        print(
            "No .blastradius/index.db found — run: blastradius analyze <repo>",
            file=sys.stderr,
        )
        sys.exit(1)

    store = Store(db_path)

    provider = None
    import os

    endpoint = os.environ.get("BLASTRADIUS_EMBEDDING_ENDPOINT", "")
    model = os.environ.get("BLASTRADIUS_EMBEDDING_MODEL", "")
    dims_str = os.environ.get("BLASTRADIUS_EMBEDDING_DIMS", "")
    if endpoint and model and dims_str:
        try:
            dims = int(dims_str)
            from blastradius.semantic.provider import OpenAIEmbeddingProvider

            provider = OpenAIEmbeddingProvider(
                endpoint=endpoint, model=model, dims=dims
            )
        except Exception:  # noqa: BLE001, S110
            pass

    as_of_reachable = None
    as_of = getattr(args, "as_of", None)
    if as_of:
        repo_root_str = store.get_meta("repo_root")
        repo_root = Path(repo_root_str) if repo_root_str else Path.cwd()
        full_hash = git_resolve(repo_root, as_of)
        if not full_hash:
            print(f"Could not resolve ref: {as_of}", file=sys.stderr)
            store.close()
            sys.exit(1)
        as_of_reachable = git_reachable(repo_root, full_hash)

    results = hybrid_search(
        store=store,
        query=args.query,
        k=args.k,
        as_of_reachable=as_of_reachable,
        provider=provider,
    )
    store.close()

    if args.json:
        # Augment JSON output with file-level aggregation
        from collections import Counter

        file_counts = Counter(r["file"] for r in results)
        files_ranked = [
            {"file": f, "symbol_hits": c} for f, c in file_counts.most_common()
        ]
        print(json.dumps({"files": files_ranked, "symbols": results}, indent=2))
    else:
        if not results:
            print("No results found.")
            return
        # File-level summary first — easier to scan for discovery queries
        from collections import Counter

        file_counts = Counter(r["file"] for r in results)
        print("Files:")
        for f, count in file_counts.most_common():
            hits = f"  ({count} symbol{'s' if count > 1 else ''})"
            print(f"  {f}{hits}")
        print("\nSymbols:")
        for r in results:
            sig = f"  {r['signature']}" if r.get("signature") else ""
            print(
                f"  {r['file']}:{r['line']}  {r['name']}  ({r.get('kind', '?')}){sig}"
            )
            if r.get("doc"):
                print(f"    {r['doc'][:120]}")


def _cmd_install_hook(args: argparse.Namespace) -> None:
    from blastradius.hook import install

    install(
        repo_path=args.repo,
        threshold=args.threshold,
        strict=args.strict,
        remove=args.remove,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blastradius",
        description="Repo dependency analyzer with blast-radius impact scoring.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── analyze ────────────────────────────────────────────────────────────
    p_analyze = sub.add_parser(
        "analyze", help="Analyze a repo and write blastradius.json"
    )
    p_analyze.add_argument(
        "repo", nargs="?", default=".", help="Path to repo root (default: .)"
    )
    p_analyze.add_argument(
        "--output", help="Output path (default: <repo>/blastradius.json)"
    )
    p_analyze.add_argument(
        "--watch", action="store_true", help="Re-index on file changes"
    )

    # ── impact ─────────────────────────────────────────────────────────────
    p_impact = sub.add_parser("impact", help="Show blast-radius impact for a file")
    p_impact.add_argument("file", help="File path to assess")
    p_impact.add_argument(
        "--index", help="Path to blastradius.json (auto-discovered if omitted)"
    )
    p_impact.add_argument("--out", help="Write markdown report to this file")
    p_impact.add_argument("--json", action="store_true", help="Output raw JSON")
    p_impact.add_argument(
        "--as-of",
        dest="as_of",
        metavar="REF",
        help="Compute blast radius at this historical commit/ref",
    )

    # ── serve ──────────────────────────────────────────────────────────────
    p_serve = sub.add_parser(
        "serve", help="Serve the visualization UI or run as MCP server"
    )
    serve_mode = p_serve.add_mutually_exclusive_group()
    serve_mode.add_argument(
        "--viz",
        action="store_true",
        default=True,
        help="Serve visualization UI (default)",
    )
    serve_mode.add_argument(
        "--mcp", action="store_true", help="Run as MCP stdio server"
    )
    p_serve.add_argument(
        "--repo", help="Repository to visualize or use as the MCP default"
    )
    p_serve.add_argument("--port", type=int, default=8080, help="Port for viz server")
    p_serve.add_argument(
        "--watch", action="store_true", help="Watch for file changes (viz mode)"
    )
    p_serve.add_argument("--output", help="blastradius.json path override (viz mode)")

    # ── symbols ────────────────────────────────────────────────────────────────
    p_sym = sub.add_parser(
        "symbols", help="Build a symbol index (functions, classes, exports)"
    )
    p_sym.add_argument(
        "repo", nargs="?", default=".", help="Path to repo root (default: .)"
    )
    p_sym.add_argument(
        "--output",
        help="Output path for symbolindex.json (default: <repo>/symbolindex.json)",
    )
    p_sym.add_argument(
        "--inline",
        action="store_true",
        help="Embed symbols into blastradius.json nodes instead of a separate file",
    )
    p_sym.add_argument(
        "--index",
        help="Path to blastradius.json for --inline mode (auto-discovered if omitted)",
    )
    p_sym.add_argument(
        "--claude-md",
        dest="claude_md",
        action="store_true",
        help="Write compressed symbol summary to CLAUDE.md (exported symbols only by default)",
    )
    p_sym.add_argument(
        "--claude-md-path",
        dest="claude_md_path",
        help="Path to CLAUDE.md (default: <repo>/CLAUDE.md)",
    )
    p_sym.add_argument(
        "--all-symbols",
        dest="all_symbols",
        action="store_true",
        help="Include non-exported symbols in --claude-md output (default: exported only)",
    )

    # ── lookup ─────────────────────────────────────────────────────────────
    p_lookup = sub.add_parser(
        "lookup", help="Find where a symbol is defined (file + line)"
    )
    p_lookup.add_argument("name", help="Symbol name to look up")
    p_lookup.add_argument(
        "--index", help="Path to symbolindex.json (auto-discovered if omitted)"
    )
    p_lookup.add_argument("--json", action="store_true", help="Output raw JSON")

    # ── dependencies ───────────────────────────────────────────────────────
    p_deps = sub.add_parser(
        "dependencies", help="Show imports and imported-by for a file"
    )
    p_deps.add_argument("file", help="File path to inspect")
    p_deps.add_argument(
        "--index", help="Path to blastradius.json (auto-discovered if omitted)"
    )
    p_deps.add_argument("--json", action="store_true", help="Output raw JSON")

    # ── high-blast ─────────────────────────────────────────────────────────
    p_hb = sub.add_parser("high-blast", help="List files above a blast score threshold")
    p_hb.add_argument(
        "--threshold", type=float, default=5.0, help="Minimum blast score (default: 5)"
    )
    p_hb.add_argument(
        "--index", help="Path to blastradius.json (auto-discovered if omitted)"
    )
    p_hb.add_argument("--json", action="store_true", help="Output raw JSON")

    # ── symbol-blast ──────────────────────────────────────────────────────────
    p_sb = sub.add_parser(
        "symbol-blast",
        help="Per-export blast radius: which importers reference each exported symbol",
    )
    p_sb.add_argument(
        "file", help="Repo-relative path to the file (e.g. lib/db/schema.ts)"
    )
    p_sb.add_argument("--json", action="store_true", help="Output raw JSON")

    # ── db ─────────────────────────────────────────────────────────────────────
    p_db = sub.add_parser("db", help="Manage the SQLite store (.blastradius/index.db)")
    p_db.add_argument("--db", help="Path to index.db (auto-discovered if omitted)")
    p_db.add_argument(
        "--json", action="store_true", help="Output raw JSON (status only)"
    )
    db_sub = p_db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser(
        "status", help="Show store schema version, last commit, and counts"
    )
    db_sub.add_parser("migrate", help="Apply pending schema migrations")

    # ── history ────────────────────────────────────────────────────────────────
    p_hist = sub.add_parser(
        "history",
        help="Backfill temporal graph data from git history (no working-tree checkouts)",
    )
    p_hist.add_argument("repo", nargs="?", default=".", help="Repo root (default: .)")
    p_hist.add_argument(
        "--since", metavar="REF", help="Only process commits after this date/ref"
    )
    p_hist.add_argument(
        "--max-commits",
        dest="max_commits",
        type=int,
        default=1000,
        help="Maximum commits to process (default: 1000)",
    )
    p_hist.add_argument("--json", action="store_true", help="Output summary as JSON")

    # ── changed-since ──────────────────────────────────────────────────────────
    p_cs = sub.add_parser(
        "changed-since",
        help="List files/edges added or removed since a commit/ref",
    )
    p_cs.add_argument("ref", help="Commit hash, branch, or tag to compare against")
    p_cs.add_argument("--repo", default=".", help="Repo root (default: .)")
    p_cs.add_argument("--db", help="Path to index.db (auto-discovered if omitted)")
    p_cs.add_argument("--json", action="store_true", help="Output raw JSON")

    # ── search ─────────────────────────────────────────────────────────────────
    p_search = sub.add_parser(
        "search",
        help="Hybrid semantic + keyword + graph symbol search",
    )
    p_search.add_argument("query", help="Natural-language or keyword query")
    p_search.add_argument(
        "--k", type=int, default=10, help="Number of results (default: 10)"
    )
    p_search.add_argument(
        "--as-of",
        dest="as_of",
        metavar="REF",
        help="Restrict results to symbols visible at this commit/ref",
    )
    p_search.add_argument("--db", help="Path to index.db (auto-discovered if omitted)")
    p_search.add_argument("--json", action="store_true", help="Output raw JSON")

    # ── install-hook ───────────────────────────────────────────────────────
    p_hook = sub.add_parser(
        "install-hook", help="Install a pre-commit hook for impact warnings"
    )
    p_hook.add_argument("--repo", default=".", help="Repo root (default: .)")
    p_hook.add_argument(
        "--threshold",
        type=int,
        default=10,
        help="Blast score warning threshold (default: 10)",
    )
    p_hook.add_argument(
        "--strict", action="store_true", help="Block commit when threshold exceeded"
    )
    p_hook.add_argument(
        "--remove", action="store_true", help="Remove the installed hook"
    )

    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = _build_parser()
    args = parser.parse_args()

    dispatch = {
        "analyze": _cmd_analyze,
        "impact": _cmd_impact,
        "serve": _cmd_serve,
        "symbols": _cmd_symbols,
        "lookup": _cmd_lookup,
        "dependencies": _cmd_dependencies,
        "high-blast": _cmd_high_blast,
        "symbol-blast": _cmd_symbol_blast,
        "install-hook": _cmd_install_hook,
        "db": _cmd_db,
        "history": _cmd_history,
        "changed-since": _cmd_changed_since,
        "search": _cmd_search,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()

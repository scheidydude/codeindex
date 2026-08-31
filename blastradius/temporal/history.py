# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

"""Git history backfill via plumbing — never touches the working tree.

Dependency rule: must not import from blastradius.graph or blastradius.semantic.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

_JS_IMPORT_RE = re.compile(
    r"""(?:from\s+|(?:require|import)\s*\(?\s*)['"]([^'"]+)['"]""",
    re.MULTILINE,
)

# Extensions we attempt to parse for imports
_PARSEABLE = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".mjs",
        ".cjs",
    }
)


# ── git plumbing helpers ──────────────────────────────────────────────────────


def _run(cmd: list, cwd: Path, timeout: int = 60) -> str:
    r = subprocess.run(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return r.stdout if r.returncode == 0 else ""


def git_log(
    root: Path,
    since: str | None = None,
    max_count: int | None = None,
) -> list[dict]:
    """Return commits oldest→newest within the requested bounds."""
    cmd = [
        "git",
        "log",
        "--format=%H%x00%ae%x00%aI%x00%cI%x00%P%x00%s",
        "--reverse",
    ]
    if since:
        cmd.append(f"--since={since}")
    if max_count:
        cmd.extend(["-n", str(max_count)])

    out = _run(cmd, root, timeout=120)
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x00")
        if len(parts) < 6:
            continue
        hash_, author, authored_at, committed_at, parents, message = parts[:6]
        parent_hash = parents.split()[0] if parents.strip() else None
        commits.append(
            {
                "hash": hash_,
                "author": author,
                "authored_at": authored_at,
                "committed_at": committed_at,
                "parent_hash": parent_hash,
                "message": message,
            }
        )
    return commits


def git_ls_tree(root: Path, commit_hash: str) -> dict[str, str]:
    """Return {repo_relative_path: blob_hash} for all files at commit."""
    out = _run(["git", "ls-tree", "-r", commit_hash], root)
    tree = {}
    for line in out.splitlines():
        # format: <mode> <type> <hash>\t<path>
        if "\t" not in line:
            continue
        meta, path = line.split("\t", 1)
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob":
            tree[path] = parts[2]
    return tree


def _git_cat_file_batch(root: Path, blob_hashes: list) -> dict[str, str]:
    """Read multiple blobs via git cat-file --batch without touching working tree."""
    if not blob_hashes:
        return {}

    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    inp = "\n".join(blob_hashes) + "\n"
    stdout, _ = proc.communicate(inp.encode(), timeout=60)

    results: dict[str, str] = {}
    pos = 0
    data = stdout

    for blob_hash in blob_hashes:
        nl = data.find(b"\n", pos)
        if nl == -1:
            break
        header = data[pos:nl].decode("utf-8", errors="replace")
        pos = nl + 1
        parts = header.split()
        if len(parts) < 3 or parts[1] == "missing":
            continue
        try:
            size = int(parts[2])
        except ValueError:
            continue
        content_bytes = data[pos : pos + size]
        results[blob_hash] = content_bytes.decode("utf-8", errors="replace")
        pos += size + 1  # trailing newline after blob content

    return results


# ── lightweight import extractor ──────────────────────────────────────────────


def _python_imports(rel_path: str, content: str, known_files: set) -> list[str]:
    """Extract Python imports that resolve to files in known_files."""
    try:
        tree = ast.parse(content, filename=rel_path)
    except SyntaxError:
        return []

    base_dir = str(Path(rel_path).parent)
    results: list[str] = []

    def _resolve(mod: str, level: int = 0) -> str | None:
        if level:
            # Relative import
            parts_base = base_dir.split("/") if base_dir != "." else []
            for _ in range(level - 1):
                if parts_base:
                    parts_base.pop()
            prefix = "/".join(parts_base)
            mod_parts = (mod or "").split(".")
            for candidate in [
                (prefix + "/" + "/".join(mod_parts) + ".py").lstrip("/"),
                (prefix + "/" + "/".join(mod_parts) + "/__init__.py").lstrip("/"),
            ]:
                if candidate in known_files:
                    return candidate
            return None

        parts = mod.split(".")
        for candidate in [
            "/".join(parts) + ".py",
            "/".join(parts) + "/__init__.py",
            base_dir + "/" + parts[0] + ".py",
        ]:
            c = candidate.lstrip("/")
            if c in known_files:
                return c
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                r = _resolve(alias.name)
                if r:
                    results.append(r)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            r = _resolve(mod, level=node.level or 0)
            if r:
                results.append(r)

    return list(set(results))


def _js_imports(rel_path: str, content: str, known_files: set) -> list[str]:
    """Extract JS/TS imports that resolve to files in known_files."""
    base_dir = str(Path(rel_path).parent)
    results: list[str] = []

    for m in _JS_IMPORT_RE.finditer(content):
        spec = m.group(1)
        if not spec.startswith("."):
            continue  # external package
        # Resolve relative path
        candidate_raw = str((Path(base_dir) / spec).as_posix())
        for candidate in [
            candidate_raw,
            candidate_raw + ".js",
            candidate_raw + ".ts",
            candidate_raw + ".jsx",
            candidate_raw + ".tsx",
            candidate_raw + "/index.js",
            candidate_raw + "/index.ts",
        ]:
            c = candidate.lstrip("/")
            if c in known_files:
                results.append(c)
                break

    return list(set(results))


def _extract_imports(rel_path: str, content: str, known_files: set) -> list[str]:
    ext = Path(rel_path).suffix.lower()
    if ext == ".py":
        return _python_imports(rel_path, content, known_files)
    if ext in {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"}:
        return _js_imports(rel_path, content, known_files)
    return []


def _is_parseable(rel_path: str) -> bool:
    return Path(rel_path).suffix.lower() in _PARSEABLE


# ── backfill ──────────────────────────────────────────────────────────────────


def backfill(
    root: Path,
    store: object,  # blastradius.store.Store — avoid circular import
    since: str | None = None,
    max_commits: int | None = 1000,
) -> tuple[int, int]:
    """Walk git history oldest→newest, populate temporal data without touching cwd.

    Returns (commits_processed, files_tracked).
    """
    commits = git_log(root, since=since, max_count=max_commits)
    if not commits:
        print("No commits found.", file=sys.stderr)
        return 0, 0

    print(f"Backfilling {len(commits)} commit(s)…", file=sys.stderr)

    # Record commits in DB first
    for c in commits:
        store.record_commit(c)

    # Forward pass: oldest→newest
    # Track per-path: first commit seen, last commit seen (while alive)
    file_first_seen: dict[str, str] = {}  # path → hash
    file_last_seen_alive: dict[str, str] = {}  # path → hash (last commit where present)

    # Track edge presence: (source, target, kind) → first hash, last alive hash
    edge_first_seen: dict[tuple, str] = {}
    edge_last_seen_alive: dict[tuple, str] = {}

    prev_tree: dict[str, str] = {}  # path → blob_hash at previous commit

    processed = 0

    for commit in commits:
        h = commit["hash"]
        curr_tree = git_ls_tree(root, h)
        known_files = set(curr_tree.keys())

        # ── file temporal tracking ────────────────────────────────────────────
        for path in curr_tree:
            if path not in file_first_seen:
                file_first_seen[path] = h
            file_last_seen_alive[path] = h

        # ── edge extraction for changed (added/modified) files ─────────────────
        added = {p for p in curr_tree if p not in prev_tree}
        modified = {
            p for p in curr_tree if p in prev_tree and curr_tree[p] != prev_tree[p]
        }
        changed = added | modified

        parseable = [p for p in changed if _is_parseable(p)]
        if parseable:
            blobs = [curr_tree[p] for p in parseable]
            contents = _git_cat_file_batch(root, blobs)
            for path in parseable:
                blob_h = curr_tree[path]
                content = contents.get(blob_h, "")
                for target in _extract_imports(path, content, known_files):
                    key = (path, target, "imports")
                    if key not in edge_first_seen:
                        edge_first_seen[key] = h
                    edge_last_seen_alive[key] = h

        # ── propagate last-seen for unchanged edges ──────────────────────────
        # Edges whose source is NOT in changed set: if source still in tree,
        # the edge still exists — advance its last-seen.
        for key in list(edge_first_seen):
            src, tgt, _kind = key
            if src not in changed and src in curr_tree and tgt in curr_tree:
                edge_last_seen_alive[key] = h

        prev_tree = curr_tree
        processed += 1

        if processed % 100 == 0:
            print(f"  {processed}/{len(commits)} commits processed", file=sys.stderr)

    # ── write temporal data to DB ─────────────────────────────────────────────
    store.apply_file_temporal(file_first_seen, file_last_seen_alive)
    store.apply_edge_temporal(edge_first_seen, edge_last_seen_alive)

    files_tracked = len(file_first_seen)
    print(
        f"Backfill complete: {processed} commits, {files_tracked} files tracked.",
        file=sys.stderr,
    )
    return processed, files_tracked

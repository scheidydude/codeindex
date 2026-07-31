"""Go repository analyzer.

Uses package-level nodes (one node per directory/package) rather than per-file,
which matches how Go developers think about their codebase.
"""
import re
from pathlib import Path

from .base import load_gitignore_patterns, is_ignored, is_skip_dir, dir_group

# ── Regexes ───────────────────────────────────────────────────────────────────
_GOMOD_MODULE_RE = re.compile(r'^module\s+(\S+)', re.MULTILINE)
_PACKAGE_RE     = re.compile(r'^package\s+(\w+)', re.MULTILINE)
# Single-line import: import "path/to/pkg"  or  import alias "path/to/pkg"
_IMPORT_SINGLE_RE = re.compile(r'^import\s+(?:\w+\s+)?["`]([^"`\s]+)["`]', re.MULTILINE)
# Import block: import ( ... )
_IMPORT_BLOCK_RE  = re.compile(r'import\s*\(([^)]+)\)', re.DOTALL)
_IMPORT_LINE_RE   = re.compile(r'(?:\w+\s+)?["`]([^"`\s]+)["`]')

# Semantic directory names
_ROUTE_DIRS  = {"handlers", "controllers", "routes", "api", "endpoints", "http", "server"}
_STORE_DIRS  = {"models", "store", "storage", "repository", "repos", "db", "database", "data", "dao"}
_CONFIG_DIRS = {"config", "cfg", "configuration", "settings", "conf"}


def collect_files(root: Path, patterns: list):
    files = []
    for p in root.rglob("*.go"):
        if is_skip_dir(p) or is_ignored(p, root, patterns):
            continue
        # Skip generated files and test-only files (optional — keep tests for now)
        files.append(p)
    return sorted(files)


def parse_module_name(root: Path):
    gomod = root / "go.mod"
    if not gomod.exists():
        return None
    m = _GOMOD_MODULE_RE.search(gomod.read_text(errors="replace"))
    return m.group(1) if m else None


def extract_imports(source: str):
    """Return deduplicated list of import paths from a .go file."""
    mods = []
    seen = set()
    # Single imports
    for m in _IMPORT_SINGLE_RE.finditer(source):
        imp = m.group(1)
        if imp not in seen:
            seen.add(imp)
            mods.append(imp)
    # Block imports
    for block in _IMPORT_BLOCK_RE.finditer(source):
        for m in _IMPORT_LINE_RE.finditer(block.group(1)):
            imp = m.group(1)
            if imp not in seen:
                seen.add(imp)
                mods.append(imp)
    return mods


def external_pkg_name(imp_path: str) -> str:
    """Condense an import path to its root module name for external packages."""
    parts = imp_path.split("/")
    # github.com/org/repo/sub/pkg → github.com/org/repo
    if parts[0] in {"github.com", "gitlab.com", "bitbucket.org", "gopkg.in"} and len(parts) >= 3:
        return "/".join(parts[:3])
    # golang.org/x/text → golang.org/x/text
    if parts[0] in {"golang.org", "google.golang.org", "k8s.io"} and len(parts) >= 2:
        return "/".join(parts[:3]) if len(parts) >= 3 else "/".join(parts[:2])
    # stdlib or simple: just the first component
    return parts[0]


def pkg_node_type(pkg_path: str, pkg_name: str) -> str:
    parts = pkg_path.lower().split("/")
    name  = pkg_name.lower()
    if any(p in _CONFIG_DIRS for p in parts) or name in _CONFIG_DIRS:
        return "config"
    if any(p in _ROUTE_DIRS for p in parts) or name in _ROUTE_DIRS:
        return "route"
    if any(p in _STORE_DIRS for p in parts) or name in _STORE_DIRS:
        return "store"
    return "module"


def analyze(root: Path, group_map: dict):
    """
    Returns (nodes, external_nodes, links_map, meta).
    Go uses package-level nodes (one per directory).
    """
    patterns    = load_gitignore_patterns(root)
    go_files    = collect_files(root, patterns)
    module_name = parse_module_name(root)

    if not go_files:
        return [], [], {}, {"total_files": 0, "total_loc": 0}

    # Group files by package directory
    packages = {}   # pkg_dir_str → {loc, imports: set(), pkg_name}
    for f in go_files:
        pkg_dir = str(f.parent.relative_to(root))
        if pkg_dir == ".":
            pkg_dir = ""
        if pkg_dir not in packages:
            packages[pkg_dir] = {"loc": 0, "imports": set(), "pkg_name": ""}

        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue

        packages[pkg_dir]["loc"] += source.count("\n") + 1

        pkg_m = _PACKAGE_RE.search(source)
        if pkg_m and not packages[pkg_dir]["pkg_name"]:
            packages[pkg_dir]["pkg_name"] = pkg_m.group(1)

        for imp in extract_imports(source):
            packages[pkg_dir]["imports"].add(imp)

    internal_pkg_dirs = set(packages.keys())
    nodes        = []
    links_map    = {}
    external_nodes = {}
    total_loc    = 0

    for pkg_dir, data in packages.items():
        loc      = data["loc"]
        total_loc += loc
        pkg_name = data["pkg_name"] or (pkg_dir.split("/")[-1] if pkg_dir else "main")
        ntype    = pkg_node_type(pkg_dir, pkg_name)
        node_id  = pkg_dir or "."

        # Group by top-level directory
        top_key = pkg_dir.split("/")[0] if pkg_dir else ""
        if top_key not in group_map:
            group_map[top_key] = len(group_map)
        group = group_map[top_key]

        nodes.append({
            "id":       node_id,
            "type":     ntype,
            "language": "go",
            "size":     loc,
            "loc":      loc,
            "group":    group,
            "imports":  len(data["imports"]),
        })

        for imp in data["imports"]:
            # Internal package?
            if module_name and imp.startswith(module_name):
                rel_pkg = imp[len(module_name):].lstrip("/")
                if rel_pkg in internal_pkg_dirs:
                    key = (node_id, rel_pkg or ".")
                    links_map[key] = links_map.get(key, 0) + 1
            else:
                # External / stdlib
                ext = external_pkg_name(imp)
                if ext not in external_nodes:
                    external_nodes[ext] = {
                        "id":       ext,
                        "type":     "import",
                        "language": "go",
                        "size":     40,
                        "loc":      0,
                        "group":    9000,
                        "imports":  0,
                    }
                key = (node_id, ext)
                links_map[key] = links_map.get(key, 0) + 1

    return nodes, list(external_nodes.values()), links_map, {
        "total_files": len(go_files),
        "total_loc":   total_loc,
    }

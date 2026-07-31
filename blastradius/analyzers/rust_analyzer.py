"""Rust repository analyzer."""
import re
import sys
from pathlib import Path

# tomllib is built-in from Python 3.11; fall back to tomli if available
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            tomllib = None

from .base import load_gitignore_patterns, is_ignored, is_skip_dir, dir_group

# ── Regexes ───────────────────────────────────────────────────────────────────
# use crate::module / use super::module / use self::module
_USE_INTERNAL_RE = re.compile(
    r'^use\s+((?:crate|super|self)(?:::\w+)+)',
    re.MULTILINE,
)
# use some_external_crate::...
_USE_EXTERNAL_RE = re.compile(r'^use\s+(\w+)::', re.MULTILINE)
# mod foo;  (declares a submodule — creates an edge to foo.rs / foo/mod.rs)
_MOD_DECL_RE = re.compile(r'^(?:pub\s+)?mod\s+(\w+)\s*;', re.MULTILINE)

_ROUTE_DIRS  = {"routes", "handlers", "controllers", "endpoints", "api"}
_STORE_DIRS  = {"models", "db", "database", "storage", "repository", "repos", "store"}
_CONFIG_NAMES = {"config", "settings", "configuration", "constants", "env"}

STDLIB_CRATES = {
    "std", "core", "alloc", "proc_macro", "test",
    "crate", "super", "self",
}


def collect_files(root: Path, patterns: list):
    files = []
    for p in root.rglob("*.rs"):
        if is_skip_dir(p) or is_ignored(p, root, patterns):
            continue
        files.append(p)
    return sorted(files)


def parse_cargo_deps(root: Path):
    """Return set of dependency crate names from Cargo.toml."""
    cargo_toml = root / "Cargo.toml"
    if not cargo_toml.exists() or tomllib is None:
        return set()
    try:
        data = tomllib.loads(cargo_toml.read_text(errors="replace"))
    except Exception:
        return set()
    deps = set()
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        deps.update(data.get(section, {}).keys())
    # Workspace members may also have their own Cargo.toml
    return deps


def node_type(path: Path) -> str:
    parts = [p.lower() for p in path.parts]
    stem  = path.stem.lower()
    if stem in _CONFIG_NAMES or any(p in _CONFIG_NAMES for p in parts):
        return "config"
    if any(p in _ROUTE_DIRS for p in parts):
        return "route"
    if any(p in _STORE_DIRS for p in parts):
        return "store"
    if stem in {"main", "lib"}:
        return "module"
    return "module"


def resolve_mod_decl(mod_name: str, file_path: Path, root: Path, all_files: set):
    """Resolve  mod foo;  to foo.rs or foo/mod.rs."""
    parent_rel = str(file_path.parent.relative_to(root))
    prefix     = parent_rel + "/" if parent_rel != "." else ""
    for candidate in (f"{prefix}{mod_name}.rs", f"{prefix}{mod_name}/mod.rs"):
        if candidate in all_files:
            return candidate
    return None


def resolve_use_internal(use_path: str, root: Path, all_files: set):
    """Resolve crate::a::b to a/b.rs or a/b/mod.rs (best-effort)."""
    if use_path.startswith("crate::"):
        rel = use_path[7:].replace("::", "/")
    elif use_path.startswith("super::") or use_path.startswith("self::"):
        return None  # relative — too complex without full module tree
    else:
        return None

    for candidate in (rel + ".rs", rel + "/mod.rs"):
        if candidate in all_files:
            return candidate
    return None


def analyze(root: Path, group_map: dict):
    patterns = load_gitignore_patterns(root)
    rs_files = collect_files(root, patterns)

    if not rs_files:
        return [], [], {}, {"total_files": 0, "total_loc": 0}

    _cargo_deps = parse_cargo_deps(root)
    all_rel     = {str(f.relative_to(root)) for f in rs_files}
    nodes       = []
    links_map   = {}
    ext_crates  = {}
    total_loc   = 0

    for f in rs_files:
        rel = str(f.relative_to(root))
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue

        loc = source.count("\n") + 1
        total_loc += loc

        # Collect all import references
        import_targets = []

        # mod foo; declarations → edges to submodule files
        for m in _MOD_DECL_RE.finditer(source):
            target = resolve_mod_decl(m.group(1), f, root, all_rel)
            if target:
                import_targets.append(("internal", target))

        # use crate:: / use super::
        for m in _USE_INTERNAL_RE.finditer(source):
            target = resolve_use_internal(m.group(1), root, all_rel)
            if target:
                import_targets.append(("internal", target))

        # use external_crate::
        for m in _USE_EXTERNAL_RE.finditer(source):
            crate = m.group(1)
            if crate not in STDLIB_CRATES:
                import_targets.append(("external", crate))

        # Deduplicate
        seen = set()
        deduped = []
        for item in import_targets:
            if item not in seen:
                seen.add(item)
                deduped.append(item)

        nodes.append({
            "id":       rel,
            "type":     node_type(f),
            "language": "rust",
            "size":     loc,
            "loc":      loc,
            "group":    dir_group(f, root, group_map),
            "imports":  len(deduped),
        })

        for kind, target in deduped:
            if kind == "internal":
                key = (rel, target)
                links_map[key] = links_map.get(key, 0) + 1
            else:
                if target not in ext_crates:
                    ext_crates[target] = {
                        "id":       target,
                        "type":     "import",
                        "language": "rust",
                        "size":     40,
                        "loc":      0,
                        "group":    9000,
                        "imports":  0,
                    }
                key = (rel, target)
                links_map[key] = links_map.get(key, 0) + 1

    return nodes, list(ext_crates.values()), links_map, {
        "total_files": len(rs_files),
        "total_loc":   total_loc,
    }

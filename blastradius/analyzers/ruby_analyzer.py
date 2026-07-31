"""Ruby repository analyzer."""
import re
from pathlib import Path

from .base import load_gitignore_patterns, is_ignored, is_skip_dir, dir_group

# ── Regexes ───────────────────────────────────────────────────────────────────
# require 'foo' / require "foo" / require_relative './foo'
_REQUIRE_RE = re.compile(r"""(?:^|;)\s*require(?:_relative)?\s+['"]([^'"]+)['"]""", re.MULTILINE)
# autoload :Name, 'path'
_AUTOLOAD_RE = re.compile(r"""autoload\s+:\w+\s*,\s*['"]([^'"]+)['"]""", re.MULTILINE)

# Semantic directory names (Rails + common patterns)
_MODEL_DIRS      = {"models"}
_CONTROLLER_DIRS = {"controllers"}
_VIEW_DIRS       = {"views", "templates"}
_SERVICE_DIRS    = {"services", "jobs", "mailers", "workers", "interactors", "operations"}
_CONFIG_STEMS    = {"config", "routes", "application", "environment", "database", "gemfile", "rakefile"}


def collect_files(root: Path, patterns: list):
    files = []
    for p in root.rglob("*.rb"):
        if is_skip_dir(p) or is_ignored(p, root, patterns):
            continue
        files.append(p)
    return sorted(files)


def detect_framework(root: Path):
    gemfile = root / "Gemfile"
    if not gemfile.exists():
        return None
    content = gemfile.read_text(errors="replace").lower()
    for fw in ("rails", "sinatra", "hanami", "roda", "padrino"):
        if f"'{fw}'" in content or f'"{fw}"' in content or f"gem '{fw}" in content or f'gem "{fw}' in content:
            return fw
    return None


def node_type(path: Path) -> str:
    parts  = [p.lower() for p in path.parts]
    stem   = path.stem.lower()

    if stem in _CONFIG_STEMS:
        return "config"
    if any(p in _MODEL_DIRS for p in parts):
        return "store"
    if any(p in _CONTROLLER_DIRS for p in parts):
        return "route"
    if any(p in _VIEW_DIRS for p in parts):
        return "component"
    if any(p in _SERVICE_DIRS for p in parts):
        return "module"
    if "config" in parts:
        return "config"
    return "module"


def resolve_internal(mod: str, file_path: Path, root: Path, all_files: set):
    """Try to map a require path to a repo-relative .rb file."""
    # require_relative uses ./  or  ../ prefix
    if mod.startswith("./") or mod.startswith("../"):
        base = file_path.parent
        raw  = (base / mod).resolve()
        for candidate in (raw.with_suffix(".rb"), raw):
            try:
                rel = str(candidate.relative_to(root))
                if rel in all_files:
                    return rel
            except ValueError:
                continue
        return None

    # Bare requires: check common load paths
    candidates = [
        mod + ".rb",
        "lib/" + mod + ".rb",
        "app/" + mod + ".rb",
    ]
    for c in candidates:
        if c in all_files:
            return c
    # Also check if any file path ends with the module path
    suffix = "/" + mod + ".rb"
    for f in all_files:
        if f.endswith(suffix):
            return f
    return None


def analyze(root: Path, group_map: dict):
    patterns  = load_gitignore_patterns(root)
    rb_files  = collect_files(root, patterns)
    framework = detect_framework(root)

    if not rb_files:
        return [], [], {}, {"total_files": 0, "total_loc": 0}

    all_rel    = {str(f.relative_to(root)) for f in rb_files}
    nodes      = []
    links_map  = {}
    ext_gems   = {}
    total_loc  = 0

    for f in rb_files:
        rel = str(f.relative_to(root))
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue

        loc = source.count("\n") + 1
        total_loc += loc

        mods  = [m.group(1) for m in _REQUIRE_RE.finditer(source)]
        mods += [m.group(1) for m in _AUTOLOAD_RE.finditer(source)]
        mods  = list(dict.fromkeys(mods))  # deduplicate

        nodes.append({
            "id":        rel,
            "type":      node_type(f),
            "language":  "ruby",
            "framework": framework,
            "size":      loc,
            "loc":       loc,
            "group":     dir_group(f, root, group_map),
            "imports":   len(mods),
        })

        for mod in mods:
            internal = resolve_internal(mod, f, root, all_rel)
            if internal:
                key = (rel, internal)
                links_map[key] = links_map.get(key, 0) + 1
            elif not (mod.startswith("./") or mod.startswith("../")):
                # External gem — use top-level name
                gem = mod.split("/")[0]
                if gem not in ext_gems:
                    ext_gems[gem] = {
                        "id":      gem,
                        "type":    "import",
                        "language":"ruby",
                        "size":    40,
                        "loc":     0,
                        "group":   9000,
                        "imports": 0,
                    }
                key = (rel, gem)
                links_map[key] = links_map.get(key, 0) + 1

    meta = {"total_files": len(rb_files), "total_loc": total_loc}
    if framework:
        meta["framework"] = framework
    return nodes, list(ext_gems.values()), links_map, meta

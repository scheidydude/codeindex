"""PHP repository analyzer."""
import json
import re
from pathlib import Path

from .base import load_gitignore_patterns, is_ignored, is_skip_dir, dir_group

# ── Regexes ───────────────────────────────────────────────────────────────────
# require/include (with or without _once)
_REQUIRE_RE   = re.compile(r"""(?:require|include)(?:_once)?\s+['"]([^'"]+)['"]""", re.IGNORECASE)
# Namespace use statement:  use Foo\Bar\Baz;  or  use Foo\Bar\Baz as Alias;
_USE_RE       = re.compile(r'^use\s+([\w\\]+)(?:\s+as\s+\w+)?\s*;', re.MULTILINE)
# Namespace declaration
_NAMESPACE_RE = re.compile(r'^namespace\s+([\w\\]+)\s*;', re.MULTILINE)

_ROUTE_DIRS    = {"controllers", "http", "routes", "api", "endpoints"}
_STORE_DIRS    = {"models", "entities", "repositories", "services"}
_STORE_STEMS   = {"model", "entity", "repository", "service", "provider"}
_CONFIG_DIRS   = {"config", "configuration"}
_CONFIG_STEMS  = {"config", "bootstrap", "app", "routes", "kernel", "middleware"}


def collect_files(root: Path, patterns: list):
    files = []
    for p in root.rglob("*.php"):
        if is_skip_dir(p) or is_ignored(p, root, patterns):
            continue
        files.append(p)
    return sorted(files)


def detect_framework(root: Path):
    composer = root / "composer.json"
    if not composer.exists():
        return None
    try:
        pkg = json.loads(composer.read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None
    deps = set(pkg.get("require", {}).keys()) | set(pkg.get("require-dev", {}).keys())
    if "laravel/framework" in deps:
        return "laravel"
    if "symfony/http-kernel" in deps or "symfony/symfony" in deps:
        return "symfony"
    if "cakephp/cakephp" in deps:
        return "cakephp"
    if "yiisoft/yii2" in deps:
        return "yii"
    if "codeigniter4/framework" in deps:
        return "codeigniter"
    return None


def read_psr4_map(root: Path):
    """Read composer.json autoload.psr-4 namespace→directory mapping."""
    composer = root / "composer.json"
    if not composer.exists():
        return {}
    try:
        pkg = json.loads(composer.read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}
    psr4 = {}
    for section in ("autoload", "autoload-dev"):
        for ns, path in pkg.get(section, {}).get("psr-4", {}).items():
            # Normalize: "App\\" → "App", path "app/" → "app"
            ns_clean = ns.rstrip("\\")
            psr4[ns_clean] = path.rstrip("/")
    return psr4


def node_type(path: Path) -> str:
    parts_lower = [p.lower() for p in path.parts]
    stem_lower  = path.stem.lower()

    if any(p in _CONFIG_DIRS for p in parts_lower) or stem_lower in _CONFIG_STEMS:
        return "config"
    if any(p in _ROUTE_DIRS for p in parts_lower) or stem_lower.endswith("controller"):
        return "route"
    if any(p in _STORE_DIRS for p in parts_lower) or any(stem_lower.endswith(s) for s in _STORE_STEMS):
        return "store"
    return "module"


def resolve_internal(mod: str, file_path: Path, root: Path, all_files: set):
    """Resolve a require/include path to a repo-relative .php file."""
    # Relative path
    base = file_path.parent
    raw  = (base / mod).resolve()
    try:
        rel = str(raw.relative_to(root))
        if rel in all_files:
            return rel
        # Try adding .php
        rel_php = str(raw.with_suffix(".php").relative_to(root))
        if rel_php in all_files:
            return rel_php
    except ValueError:
        pass
    return None


def namespace_to_path(ns_class: str, psr4_map: dict, all_files: set, root: Path):
    """Try to resolve a PSR-4 namespaced class to a file path."""
    parts = ns_class.replace("\\", "/")
    # Try each PSR-4 prefix
    for ns, base_dir in psr4_map.items():
        ns_path = ns.replace("\\", "/")
        if parts.startswith(ns_path + "/") or parts == ns_path:
            rel_suffix = parts[len(ns_path):].lstrip("/")
            candidate  = f"{base_dir}/{rel_suffix}.php"
            if candidate in all_files:
                return candidate
    # Fallback: check if any file path suffix matches
    suffix = "/" + parts.split("/")[-1] + ".php"
    for f in all_files:
        if f.endswith(suffix):
            return f
    return None


def analyze(root: Path, group_map: dict):
    patterns  = load_gitignore_patterns(root)
    php_files = collect_files(root, patterns)
    framework = detect_framework(root)
    psr4_map  = read_psr4_map(root)

    if not php_files:
        return [], [], {}, {"total_files": 0, "total_loc": 0}

    all_rel   = {str(f.relative_to(root)) for f in php_files}
    nodes     = []
    links_map = {}
    ext_pkgs  = {}
    total_loc = 0

    for f in php_files:
        rel = str(f.relative_to(root))
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue

        loc = source.count("\n") + 1
        total_loc += loc

        require_mods = [m.group(1) for m in _REQUIRE_RE.finditer(source)]
        use_mods     = [m.group(1) for m in _USE_RE.finditer(source)]
        all_mods     = list(dict.fromkeys(require_mods + use_mods))

        nodes.append({
            "id":        rel,
            "type":      node_type(f),
            "language":  "php",
            "framework": framework,
            "size":      loc,
            "loc":       loc,
            "group":     dir_group(f, root, group_map),
            "imports":   len(all_mods),
        })

        for mod in require_mods:
            internal = resolve_internal(mod, f, root, all_rel)
            if internal:
                key = (rel, internal)
                links_map[key] = links_map.get(key, 0) + 1

        for ns_class in use_mods:
            internal = namespace_to_path(ns_class, psr4_map, all_rel, root)
            if internal:
                key = (rel, internal)
                links_map[key] = links_map.get(key, 0) + 1
            else:
                # External vendor package — top two namespace parts
                parts = ns_class.split("\\")
                pkg   = "\\".join(parts[:2]) if len(parts) >= 2 else parts[0]
                if pkg not in ext_pkgs:
                    ext_pkgs[pkg] = {
                        "id":       pkg,
                        "type":     "import",
                        "language": "php",
                        "size":     40,
                        "loc":      0,
                        "group":    9000,
                        "imports":  0,
                    }
                key = (rel, pkg)
                links_map[key] = links_map.get(key, 0) + 1

    meta = {"total_files": len(php_files), "total_loc": total_loc}
    if framework:
        meta["framework"] = framework
    return nodes, list(ext_pkgs.values()), links_map, meta

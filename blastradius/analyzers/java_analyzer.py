"""Java / Kotlin repository analyzer.

Uses a two-pass approach:
  1. Scan all files to build a map of fully-qualified class name → file path
  2. Resolve import statements against that map
"""
import re
from pathlib import Path

from .base import load_gitignore_patterns, is_ignored, is_skip_dir, dir_group

# ── Regexes ───────────────────────────────────────────────────────────────────
_JAVA_PACKAGE_RE  = re.compile(r'^package\s+([\w.]+)\s*;',  re.MULTILINE)
_KOTLIN_PACKAGE_RE= re.compile(r'^package\s+([\w.]+)',       re.MULTILINE)
_JAVA_IMPORT_RE   = re.compile(r'^import\s+(?:static\s+)?([\w.]+)\s*;', re.MULTILINE)
_KOTLIN_IMPORT_RE = re.compile(r'^import\s+([\w.]+)',         re.MULTILINE)

JAVA_EXTENSIONS = {".java", ".kt", ".kts"}

_ROUTE_STEMS  = {"controller", "resource", "endpoint", "rest", "api", "servlet"}
_STORE_STEMS  = {"service", "repository", "dao", "mapper", "store", "repo",
                 "serviceimpl", "repositoryimpl"}
_CONFIG_STEMS = {"config", "configuration", "application", "properties",
                 "bootstrap", "settings"}


def collect_files(root: Path, patterns: list):
    files = []
    for ext in JAVA_EXTENSIONS:
        for p in root.rglob(f"*{ext}"):
            if is_skip_dir(p) or is_ignored(p, root, patterns):
                continue
            # Skip generated files (common patterns)
            if any(part in {"generated", "generated-sources", "build", "target"} for part in p.parts):
                continue
            files.append(p)
    return sorted(set(files))


def detect_language(path: Path) -> str:
    return "kotlin" if path.suffix.lower() in {".kt", ".kts"} else "java"


def read_package(source: str, is_kotlin: bool) -> str:
    pattern = _KOTLIN_PACKAGE_RE if is_kotlin else _JAVA_PACKAGE_RE
    m = pattern.search(source)
    return m.group(1) if m else ""


def extract_imports(source: str, is_kotlin: bool):
    pattern = _KOTLIN_IMPORT_RE if is_kotlin else _JAVA_IMPORT_RE
    return [m.group(1) for m in pattern.finditer(source)]


def node_type(path: Path) -> str:
    stem_lower  = path.stem.lower()
    parts_lower = [p.lower() for p in path.parts]

    # Config
    if any(s in stem_lower for s in _CONFIG_STEMS) or any(p in _CONFIG_STEMS for p in parts_lower):
        return "config"
    # Test files
    if "test" in parts_lower or stem_lower.endswith("test") or stem_lower.endswith("tests") \
            or stem_lower.endswith("spec"):
        return "module"
    # Route/Controller
    if any(stem_lower.endswith(s) for s in _ROUTE_STEMS):
        return "route"
    if "controllers" in parts_lower or "controller" in parts_lower:
        return "route"
    # Service/Repository → store
    if any(stem_lower.endswith(s) for s in _STORE_STEMS):
        return "store"
    if "repository" in parts_lower or "repositories" in parts_lower or "service" in parts_lower:
        return "store"

    return "module"


def fqn_to_file_path(fqn: str) -> str:
    """Convert fully-qualified class name to relative file path (without extension)."""
    return fqn.replace(".", "/")


def analyze(root: Path, group_map: dict):
    patterns = load_gitignore_patterns(root)
    jk_files = collect_files(root, patterns)

    if not jk_files:
        return [], [], {}, {"total_files": 0, "total_loc": 0}

    all_rel = {str(f.relative_to(root)) for f in jk_files}

    # ── Pass 1: build FQN → rel_path map ─────────────────────────────────────
    fqn_map = {}   # "com.example.pkg.Foo" → "src/main/java/com/example/pkg/Foo.java"
    file_packages = {}  # rel_path → package string

    for f in jk_files:
        rel = str(f.relative_to(root))
        is_kotlin = detect_language(f) == "kotlin"
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue
        pkg = read_package(source, is_kotlin)
        file_packages[rel] = pkg
        if pkg:
            fqn = f"{pkg}.{f.stem}"
            fqn_map[fqn] = rel

    # ── Pass 2: analyze imports ───────────────────────────────────────────────
    nodes     = []
    links_map = {}
    ext_pkgs  = {}
    total_loc = 0

    for f in jk_files:
        rel = str(f.relative_to(root))
        is_kotlin = detect_language(f) == "kotlin"
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue

        loc = source.count("\n") + 1
        total_loc += loc

        imports = extract_imports(source, is_kotlin)

        nodes.append({
            "id":       rel,
            "type":     node_type(f),
            "language": detect_language(f),
            "size":     loc,
            "loc":      loc,
            "group":    dir_group(f, root, group_map),
            "imports":  len(imports),
        })

        for imp in imports:
            # Check exact FQN match
            if imp in fqn_map:
                key = (rel, fqn_map[imp])
                links_map[key] = links_map.get(key, 0) + 1
                continue

            # Check wildcard: com.example.pkg.* → any file in that package
            if imp.endswith(".*"):
                pkg_prefix = imp[:-2]
                matched = False
                for fqn, path in fqn_map.items():
                    if fqn.startswith(pkg_prefix + "."):
                        key = (rel, path)
                        links_map[key] = links_map.get(key, 0) + 1
                        matched = True
                if matched:
                    continue

            # External — use top-level package name (e.g., org.springframework)
            top_pkg = ".".join(imp.split(".")[:2]) if "." in imp else imp
            if top_pkg not in ext_pkgs:
                ext_pkgs[top_pkg] = {
                    "id":       top_pkg,
                    "type":     "import",
                    "language": detect_language(f),
                    "size":     40,
                    "loc":      0,
                    "group":    9000,
                    "imports":  0,
                }
            key = (rel, top_pkg)
            links_map[key] = links_map.get(key, 0) + 1

    return nodes, list(ext_pkgs.values()), links_map, {
        "total_files": len(jk_files),
        "total_loc":   total_loc,
    }

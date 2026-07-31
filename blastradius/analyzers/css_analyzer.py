"""CSS / SCSS / Less stylesheet analyzer."""
import re
from pathlib import Path

from .base import load_gitignore_patterns, is_ignored, is_skip_dir, dir_group

CSS_EXTENSIONS = {".css", ".scss", ".sass", ".less", ".styl"}

# @import 'X' / @import "X" / @import url('X')
_CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\s*\(\s*)?['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
# SCSS @use 'X' and @forward 'X'
_SCSS_USE_RE = re.compile(r"""@(?:use|forward)\s+['"]([^'"]+)['"]""", re.IGNORECASE)


def collect_files(root: Path, patterns: list):
    seen = set()
    for ext in CSS_EXTENSIONS:
        for p in root.rglob(f"*{ext}"):
            if is_skip_dir(p) or is_ignored(p, root, patterns):
                continue
            seen.add(p)
    return sorted(seen)


def detect_language(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".scss", ".sass"}:
        return "scss"
    if suffix == ".less":
        return "less"
    return "css"


def extract_imports(source: str):
    """Return deduplicated list of stylesheet paths referenced in a file."""
    seen = set()
    mods = []
    for pattern in (_CSS_IMPORT_RE, _SCSS_USE_RE):
        for m in pattern.finditer(source):
            mod = m.group(1)
            # Skip external URLs and data URIs
            if mod.startswith("http") or mod.startswith("//") or mod.startswith("data:"):
                continue
            if mod not in seen:
                seen.add(mod)
                mods.append(mod)
    return mods


def resolve_internal(mod: str, file_path: Path, root: Path, all_files: set):
    """Resolve a CSS @import path to a repo-relative file path."""
    base = file_path.parent
    raw = (base / mod).resolve()

    # Try exact match first (may already have extension)
    try:
        rel = str(raw.relative_to(root))
        if rel in all_files:
            return rel
    except ValueError:
        pass

    # Try adding CSS extensions
    for ext in CSS_EXTENSIONS:
        try:
            rel = str(raw.with_suffix(ext).relative_to(root))
            if rel in all_files:
                return rel
        except ValueError:
            continue

    # SCSS partial convention: _filename.scss
    for ext in (".scss", ".sass"):
        try:
            partial = raw.parent / f"_{raw.stem}{ext}"
            rel = str(partial.relative_to(root))
            if rel in all_files:
                return rel
        except ValueError:
            continue

    return None


def analyze(root: Path, group_map: dict):
    """
    Returns (nodes, external_nodes, links_map, meta).
    links_map keys are (source_rel, target_rel) tuples.
    """
    patterns = load_gitignore_patterns(root)
    css_files = collect_files(root, patterns)

    if not css_files:
        return [], [], {}, {"total_files": 0, "total_loc": 0}

    all_rel = {str(f.relative_to(root)) for f in css_files}
    nodes = []
    links_map = {}
    total_loc = 0

    for f in css_files:
        rel = str(f.relative_to(root))
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue

        loc = source.count("\n") + 1
        total_loc += loc

        mods = extract_imports(source)
        lang = detect_language(f)

        nodes.append({
            "id": rel,
            "type": "style",
            "language": lang,
            "size": loc,
            "loc": loc,
            "group": dir_group(f, root, group_map),
            "imports": len(mods),
        })

        for mod in mods:
            internal = resolve_internal(mod, f, root, all_rel)
            if internal:
                key = (rel, internal)
                links_map[key] = links_map.get(key, 0) + 1
            # CSS external imports (CDN fonts etc.) are not tracked as nodes

    return nodes, [], links_map, {
        "total_files": len(css_files),
        "total_loc": total_loc,
    }

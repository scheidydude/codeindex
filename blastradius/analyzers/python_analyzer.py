"""Python repository analyzer (AST-based)."""
import ast
import sys
from pathlib import Path

from .base import load_gitignore_patterns, is_ignored, is_skip_dir, dir_group

CONFIG_NAMES = {"config", "settings", "constants", "env", "configuration", "conf"}
STDLIB_TOP = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()


def collect_files(root: Path, patterns: list) -> list[Path]:
    files = []
    for p in sorted(root.rglob("*.py")):
        if is_skip_dir(p):
            continue
        if is_ignored(p, root, patterns):
            continue
        files.append(p)
    return files


def node_type(path: Path) -> str:
    return "config" if path.stem.lower() in CONFIG_NAMES else "module"


def parse_imports(tree) -> list[tuple[str, str]]:
    """Return list of (kind, module_name) from an AST."""
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(("import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            dots = "." * (node.level or 0)
            if node.module:
                imports.append(("from", dots + node.module))
            elif node.level:
                # e.g. `from . import module1, module2` — module is None
                for alias in node.names:
                    imports.append(("from", dots + alias.name))
    return imports


def resolve_internal(mod_name: str, file_path: Path, root: Path, all_files: set):
    """Try to map a module name to a relative .py path in the repo."""
    # Handle relative imports (leading dots)
    if mod_name.startswith("."):
        level = len(mod_name) - len(mod_name.lstrip("."))
        mod_part = mod_name[level:]
        base = file_path.parent
        for _ in range(level - 1):
            base = base.parent
        if mod_part:
            parts = mod_part.split(".")
            try:
                candidates = [
                    str((base / "/".join(parts)).relative_to(root)) + ".py",
                    str((base / "/".join(parts) / "__init__.py").relative_to(root)),
                ]
            except ValueError:
                return None
        else:
            # bare `from . import X` with no module part — points to the package itself
            try:
                c = str((base / "__init__.py").relative_to(root))
                return c if c in all_files else None
            except ValueError:
                return None
        for c in candidates:
            if c in all_files:
                return c
        return None

    parts = mod_name.split(".")
    candidates = [
        "/".join(parts) + ".py",
        "/".join(parts) + "/__init__.py",
    ]
    for c in candidates:
        if c in all_files:
            return c
    rel_base = str(file_path.parent.relative_to(root))
    if rel_base != ".":
        for c in candidates:
            full = f"{rel_base}/{c}"
            if full in all_files:
                return full
    return None


def analyze(root: Path, group_map: dict):
    """
    Returns (nodes, external_nodes, links_map, meta).
    links_map keys are (source_rel, target_rel) tuples.
    """
    patterns = load_gitignore_patterns(root)
    py_files = collect_files(root, patterns)

    all_rel: set[str] = {str(f.relative_to(root)) for f in py_files}
    nodes = []
    links_map: dict[tuple[str, str], int] = {}
    external_nodes: dict[str, dict] = {}
    total_loc = 0

    for f in py_files:
        rel = str(f.relative_to(root))
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue

        loc = source.count("\n") + 1
        total_loc += loc

        try:
            tree = ast.parse(source, filename=str(f))
        except SyntaxError:
            tree = None

        imports_list = parse_imports(tree) if tree else []

        nodes.append({
            "id": rel,
            "type": node_type(f),
            "language": "python",
            "size": loc,
            "loc": loc,
            "group": dir_group(f, root, group_map),
            "imports": len(imports_list),
        })

        for _kind, mod in imports_list:
            top_level = mod.split(".")[0]
            internal = resolve_internal(mod, f, root, all_rel)
            if internal:
                key = (rel, internal)
                links_map[key] = links_map.get(key, 0) + 1
            else:
                if top_level not in external_nodes:
                    external_nodes[top_level] = {
                        "id": top_level,
                        "type": "import",
                        "language": "python",
                        "size": 40,
                        "loc": 0,
                        "group": 9000,
                        "imports": 0,
                    }
                key = (rel, top_level)
                links_map[key] = links_map.get(key, 0) + 1

    return nodes, list(external_nodes.values()), links_map, {
        "total_files": len(py_files),
        "total_loc": total_loc,
    }

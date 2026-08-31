# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

"""Go repository analyzer with package-level dependency nodes."""

from dataclasses import dataclass, field
from pathlib import Path

from .base import is_ignored, is_skip_dir, load_gitignore_patterns

_ROUTE_DIRS = {
    "handlers",
    "controllers",
    "routes",
    "api",
    "endpoints",
    "http",
    "server",
}
_STORE_DIRS = {
    "models",
    "store",
    "storage",
    "repository",
    "repos",
    "db",
    "database",
    "data",
    "dao",
}
_CONFIG_DIRS = {"config", "cfg", "configuration", "settings", "conf"}


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


@dataclass
class _GoModule:
    root: Path
    name: str
    requires: set[str] = field(default_factory=set)
    replacements: dict[str, Path] = field(default_factory=dict)


@dataclass
class _GoWorkspace:
    root: Path
    replacements: dict[str, Path] = field(default_factory=dict)


def collect_files(root: Path, patterns: list):
    files = []
    for path in root.rglob("*.go"):
        if is_skip_dir(path) or is_ignored(path, root, patterns):
            continue
        files.append(path)
    return sorted(files)


def _identifier_start(char: str) -> bool:
    return char == "_" or char.isalpha()


def _identifier_part(char: str) -> bool:
    return char == "_" or char.isalnum()


def _read_quoted(source: str, start: int, quote: str) -> tuple[str, int]:
    value = []
    index = start + 1
    while index < len(source):
        char = source[index]
        if quote == '"' and char == "\\" and index + 1 < len(source):
            value.append(source[index + 1])
            index += 2
            continue
        if char == quote:
            return "".join(value), index + 1
        value.append(char)
        index += 1
    return "".join(value), index


def _tokenize(source: str) -> list[_Token]:
    tokens = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline == -1 else newline + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            index = len(source) if end == -1 else end + 2
            continue
        if char in {'"', "`"}:
            value, index = _read_quoted(source, index, char)
            tokens.append(_Token("string", value))
            continue
        if char == "'":
            _value, index = _read_quoted(source, index, char)
            continue
        if _identifier_start(char):
            end = index + 1
            while end < len(source) and _identifier_part(source[end]):
                end += 1
            tokens.append(_Token("identifier", source[index:end]))
            index = end
            continue
        tokens.append(_Token("punctuation", char))
        index += 1
    return tokens


def _scan_go_source(source: str) -> tuple[str | None, list[str]]:
    tokens = _tokenize(source)
    package_name = None
    imports = []
    seen = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            token.kind == "identifier"
            and token.value == "package"
            and index + 1 < len(tokens)
            and tokens[index + 1].kind == "identifier"
            and package_name is None
        ):
            package_name = tokens[index + 1].value
            index += 2
            continue
        if (
            token.kind != "identifier"
            or token.value != "import"
            or index + 1 >= len(tokens)
        ):
            index += 1
            continue

        index += 1
        if tokens[index].kind == "punctuation" and tokens[index].value == "(":
            depth = 1
            index += 1
            while index < len(tokens) and depth:
                current = tokens[index]
                if current.kind == "punctuation" and current.value == "(":
                    depth += 1
                elif current.kind == "punctuation" and current.value == ")":
                    depth -= 1
                elif (
                    current.kind == "string"
                    and depth == 1
                    and current.value not in seen
                ):
                    seen.add(current.value)
                    imports.append(current.value)
                index += 1
            continue

        for candidate in tokens[index : index + 3]:
            if candidate.kind == "string":
                if candidate.value not in seen:
                    seen.add(candidate.value)
                    imports.append(candidate.value)
                break
        index += 1
    return package_name, imports


def extract_imports(source: str):
    """Return deduplicated import paths from a Go source file."""
    return _scan_go_source(source)[1]


def _strip_comment(line: str) -> str:
    quoted = False
    raw = False
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quoted and char == "\\":
            escaped = True
            continue
        if not raw and char == '"':
            quoted = not quoted
            continue
        if not quoted and char == "`":
            raw = not raw
            continue
        if not quoted and not raw and line.startswith("//", index):
            return line[:index]
    return line


def _directive_lines(path: Path):
    mode = None
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        if mode:
            if line == ")":
                mode = None
                continue
            yield mode, line
            continue
        parts = line.split(None, 1)
        directive = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
        if rest == "(":
            mode = directive
            continue
        yield directive, rest


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "`"}:
        return value[1:-1]
    return value


def _parse_replace(value: str, base: Path) -> tuple[str, Path] | None:
    if "=>" not in value:
        return None
    left, right = value.split("=>", 1)
    left_parts = left.split()
    right_parts = right.split()
    if not left_parts or not right_parts:
        return None
    old_module = _unquote(left_parts[0])
    replacement = _unquote(right_parts[0])
    replacement_path = Path(replacement)
    if not replacement_path.is_absolute() and not replacement.startswith("."):
        return None
    if not replacement_path.is_absolute():
        replacement_path = base / replacement_path
    return old_module, replacement_path.resolve()


def _parse_module(path: Path) -> _GoModule | None:
    name = None
    requires = set()
    replacements = {}
    for directive, value in _directive_lines(path):
        parts = value.split()
        if directive == "module" and parts:
            name = _unquote(parts[0])
        elif directive == "require" and parts:
            requires.add(_unquote(parts[0]))
        elif directive == "replace":
            replacement = _parse_replace(value, path.parent)
            if replacement:
                old_module, target = replacement
                replacements[old_module] = target
    if not name:
        return None
    return _GoModule(path.parent.resolve(), name, requires, replacements)


def _parse_workspace(path: Path) -> _GoWorkspace:
    replacements = {}
    for directive, value in _directive_lines(path):
        if directive != "replace":
            continue
        replacement = _parse_replace(value, path.parent)
        if replacement:
            old_module, target = replacement
            replacements[old_module] = target
    return _GoWorkspace(path.parent.resolve(), replacements)


def _discover_modules(
    root: Path, patterns: list
) -> tuple[list[_GoModule], list[_GoWorkspace]]:
    modules = []
    workspaces = []
    for path in sorted(root.rglob("go.mod")):
        if is_skip_dir(path) or is_ignored(path, root, patterns):
            continue
        module = _parse_module(path)
        if module:
            modules.append(module)
    for path in sorted(root.rglob("go.work")):
        if is_skip_dir(path) or is_ignored(path, root, patterns):
            continue
        workspaces.append(_parse_workspace(path))
    return modules, workspaces


def parse_module_name(root: Path):
    """Return the module name from the root go.mod, if present."""
    module = _parse_module(root / "go.mod") if (root / "go.mod").exists() else None
    return module.name if module else None


def external_pkg_name(imp_path: str) -> str:
    """Condense an import path to its root module name for external packages."""
    parts = imp_path.split("/")
    if (
        parts[0] in {"github.com", "gitlab.com", "bitbucket.org", "gopkg.in"}
        and len(parts) >= 3
    ):
        return "/".join(parts[:3])
    if parts[0] in {"golang.org", "google.golang.org", "k8s.io"} and len(parts) >= 2:
        return "/".join(parts[:3]) if len(parts) >= 3 else "/".join(parts[:2])
    return parts[0]


def pkg_node_type(pkg_path: str, pkg_name: str) -> str:
    parts = pkg_path.lower().split("/")
    name = pkg_name.lower()
    if any(part in _CONFIG_DIRS for part in parts) or name in _CONFIG_DIRS:
        return "config"
    if any(part in _ROUTE_DIRS for part in parts) or name in _ROUTE_DIRS:
        return "route"
    if any(part in _STORE_DIRS for part in parts) or name in _STORE_DIRS:
        return "store"
    return "module"


def _contains(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def _nearest_module(path: Path, modules: list[_GoModule]) -> _GoModule | None:
    candidates = [module for module in modules if _contains(path, module.root)]
    return max(candidates, key=lambda module: len(module.root.parts), default=None)


def _nearest_workspace(
    path: Path, workspaces: list[_GoWorkspace]
) -> _GoWorkspace | None:
    candidates = [
        workspace for workspace in workspaces if _contains(path, workspace.root)
    ]
    return max(
        candidates, key=lambda workspace: len(workspace.root.parts), default=None
    )


def _path_matches(import_path: str, module_path: str) -> bool:
    return import_path == module_path or import_path.startswith(module_path + "/")


def _relative_directory(path: Path, root: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(root)
    except ValueError:
        return None
    value = relative.as_posix()
    return "" if value == "." else value


def _local_candidates(
    import_path: str,
    source_module: _GoModule | None,
    source_directory: Path,
    modules: list[_GoModule],
    workspaces: list[_GoWorkspace],
):
    workspace = _nearest_workspace(source_directory, workspaces)
    if workspace:
        for module_path, target_root in workspace.replacements.items():
            if _path_matches(import_path, module_path):
                yield 0, module_path, target_root
    if source_module:
        for module_path, target_root in source_module.replacements.items():
            if _path_matches(import_path, module_path):
                yield 1, module_path, target_root
    for module in modules:
        if _path_matches(import_path, module.name):
            yield 2, module.name, module.root


def _resolve_import(
    import_path: str,
    source_module: _GoModule | None,
    source_directory: Path,
    modules: list[_GoModule],
    workspaces: list[_GoWorkspace],
    internal_packages: set[str],
    root: Path,
) -> tuple[str, str]:
    candidates = list(
        _local_candidates(
            import_path, source_module, source_directory, modules, workspaces
        )
    )
    if candidates:
        precedence, module_path, target_root = min(
            candidates, key=lambda item: (item[0], -len(item[1]))
        )
        _ = precedence
        suffix = import_path[len(module_path) :].lstrip("/")
        target_directory = target_root / suffix if suffix else target_root
        relative = _relative_directory(target_directory, root)
        node_id = relative or "."
        if relative is not None and node_id in internal_packages:
            return "internal", node_id
        return "external", module_path

    if source_module:
        requirements = [
            requirement
            for requirement in source_module.requires
            if _path_matches(import_path, requirement)
        ]
        if requirements:
            return "external", max(requirements, key=len)
    return "external", external_pkg_name(import_path)


def analyze(root: Path, group_map: dict):
    patterns = load_gitignore_patterns(root)
    go_files = collect_files(root, patterns)
    if not go_files:
        return [], [], {}, {"total_files": 0, "total_loc": 0}

    modules, workspaces = _discover_modules(root, patterns)
    packages = {}
    for path in go_files:
        package_directory = path.parent.relative_to(root).as_posix()
        if package_directory == ".":
            package_directory = ""
        data = packages.setdefault(
            package_directory,
            {"loc": 0, "imports": set(), "pkg_name": "", "module": None},
        )
        try:
            source = path.read_text(errors="replace")
        except OSError:
            continue
        package_name, imports = _scan_go_source(source)
        data["loc"] += source.count("\n") + 1
        if package_name and not data["pkg_name"]:
            data["pkg_name"] = package_name
        data["imports"].update(imports)
        if data["module"] is None:
            data["module"] = _nearest_module(path.parent.resolve(), modules)

    internal_packages = {package or "." for package in packages}
    nodes = []
    links_map = {}
    external_nodes = {}
    total_loc = 0

    for package_directory, data in packages.items():
        loc = data["loc"]
        total_loc += loc
        package_name = data["pkg_name"] or (
            package_directory.split("/")[-1] if package_directory else "main"
        )
        node_id = package_directory or "."
        top_key = package_directory.split("/")[0] if package_directory else ""
        if top_key not in group_map:
            group_map[top_key] = len(group_map)
        nodes.append(
            {
                "id": node_id,
                "type": pkg_node_type(package_directory, package_name),
                "language": "go",
                "size": loc,
                "loc": loc,
                "group": group_map[top_key],
                "imports": len(data["imports"]),
            }
        )

        source_directory = (root / package_directory).resolve()
        for import_path in data["imports"]:
            kind, target = _resolve_import(
                import_path,
                data["module"],
                source_directory,
                modules,
                workspaces,
                internal_packages,
                root,
            )
            if kind == "external" and target not in external_nodes:
                external_nodes[target] = {
                    "id": target,
                    "type": "import",
                    "language": "go",
                    "size": 40,
                    "loc": 0,
                    "group": 9000,
                    "imports": 0,
                }
            key = (node_id, target)
            links_map[key] = links_map.get(key, 0) + 1

    return (
        nodes,
        list(external_nodes.values()),
        links_map,
        {"total_files": len(go_files), "total_loc": total_loc},
    )

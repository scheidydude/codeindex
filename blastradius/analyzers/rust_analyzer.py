# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

"""Rust repository analyzer."""

import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

from .base import dir_group, is_ignored, is_skip_dir, load_gitignore_patterns

_ROUTE_DIRS = {"routes", "handlers", "controllers", "endpoints", "api"}
_STORE_DIRS = {"models", "db", "database", "storage", "repository", "repos", "store"}
_CONFIG_NAMES = {"config", "settings", "configuration", "constants", "env"}

STDLIB_CRATES = {
    "std",
    "core",
    "alloc",
    "proc_macro",
    "test",
    "crate",
    "super",
    "self",
}


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


@dataclass(frozen=True)
class _ModDecl:
    name: str
    inline_scope: tuple[str, ...]
    path_override: str | None = None


@dataclass
class _RustItems:
    uses: list[tuple[tuple[str, ...], tuple[str, ...]]] = field(default_factory=list)
    extern_crates: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    mods: list[_ModDecl] = field(default_factory=list)


@dataclass
class _CargoPackage:
    root: Path
    data: dict
    name: str
    lib_name: str
    targets: list[_CrateTarget] = field(default_factory=list)
    local_dependencies: dict[str, _CargoPackage] = field(default_factory=dict)


@dataclass
class _CrateTarget:
    root_file: Path | None
    crate_name: str
    kind: str
    package: _CargoPackage | None
    modules: dict[tuple[str, ...], list[str]] = field(default_factory=dict)
    declared_modules: dict[tuple[str, ...], list[str]] = field(default_factory=dict)
    file_modules: dict[str, list[tuple[str, ...]]] = field(default_factory=dict)


def collect_files(root: Path, patterns: list):
    files = []
    for path in root.rglob("*.rs"):
        if is_skip_dir(path) or is_ignored(path, root, patterns):
            continue
        files.append(path)
    return sorted(files)


def _read_toml(path: Path) -> dict:
    if tomllib is None:
        return {}
    try:
        return tomllib.loads(path.read_text(errors="replace"))
    except Exception:  # noqa: BLE001
        return {}


def parse_cargo_deps(root: Path):
    """Return dependency aliases declared by the root Cargo manifest."""
    data = _read_toml(root / "Cargo.toml")
    deps = set()
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        deps.update(data.get(section, {}).keys())
    return deps


def node_type(path: Path) -> str:
    parts = [part.lower() for part in path.parts]
    stem = path.stem.lower()
    if stem in _CONFIG_NAMES or any(part in _CONFIG_NAMES for part in parts):
        return "config"
    if any(part in _ROUTE_DIRS for part in parts):
        return "route"
    if any(part in _STORE_DIRS for part in parts):
        return "store"
    return "module"


def _identifier_start(char: str) -> bool:
    return char == "_" or char.isalpha()


def _identifier_part(char: str) -> bool:
    return char == "_" or char.isalnum()


def _read_quoted(source: str, start: int, quote: str) -> tuple[str, int]:
    value = []
    index = start + 1
    while index < len(source):
        char = source[index]
        if char == "\\" and index + 1 < len(source):
            value.append(source[index + 1])
            index += 2
            continue
        if char == quote:
            return "".join(value), index + 1
        value.append(char)
        index += 1
    return "".join(value), index


def _read_raw_string(source: str, start: int) -> tuple[str, int] | None:
    index = start
    if source.startswith("br", index):
        index += 2
    elif source.startswith("r", index):
        index += 1
    else:
        return None
    hashes = 0
    while index < len(source) and source[index] == "#":
        hashes += 1
        index += 1
    if index >= len(source) or source[index] != '"':
        return None
    content_start = index + 1
    delimiter = '"' + ("#" * hashes)
    end = source.find(delimiter, content_start)
    if end == -1:
        return source[content_start:], len(source)
    return source[content_start:end], end + len(delimiter)


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
            depth = 1
            index += 2
            while index < len(source) and depth:
                if source.startswith("/*", index):
                    depth += 1
                    index += 2
                elif source.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            continue
        raw = _read_raw_string(source, index)
        if raw is not None:
            value, index = raw
            tokens.append(_Token("string", value))
            continue
        if char == '"' or (char == "b" and source[index : index + 2] == 'b"'):
            quote_index = index if char == '"' else index + 1
            value, index = _read_quoted(source, quote_index, '"')
            tokens.append(_Token("string", value))
            continue
        if char == "'":
            closing = source.find("'", index + 1)
            if closing != -1 and closing - index <= 6:
                index = closing + 1
                continue
        if (
            source.startswith("r#", index)
            and index + 2 < len(source)
            and _identifier_start(source[index + 2])
        ):
            end = index + 3
            while end < len(source) and _identifier_part(source[end]):
                end += 1
            tokens.append(_Token("identifier", source[index + 2 : end]))
            index = end
            continue
        if _identifier_start(char):
            end = index + 1
            while end < len(source) and _identifier_part(source[end]):
                end += 1
            tokens.append(_Token("identifier", source[index:end]))
            index = end
            continue
        if source.startswith("::", index):
            tokens.append(_Token("punctuation", "::"))
            index += 2
            continue
        tokens.append(_Token("punctuation", char))
        index += 1
    return tokens


def _split_top_level(tokens: list[_Token]) -> list[list[_Token]]:
    groups = []
    start = 0
    depth = 0
    for index, token in enumerate(tokens):
        if token.value == "{":
            depth += 1
        elif token.value == "}":
            depth -= 1
        elif token.value == "," and depth == 0:
            groups.append(tokens[start:index])
            start = index + 1
    groups.append(tokens[start:])
    return [group for group in groups if group]


def _path_parts(tokens: list[_Token]) -> tuple[str, ...]:
    parts = []
    for token in tokens:
        if token.value == "as":
            break
        if token.kind == "identifier":
            parts.append(token.value)
    return tuple(parts)


def _expand_use_tree(
    tokens: list[_Token], prefix: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    while tokens and tokens[0].value == "::":
        tokens = tokens[1:]
    brace_index = None
    depth = 0
    close_index = None
    for index, token in enumerate(tokens):
        if token.value == "{":
            if depth == 0 and brace_index is None:
                brace_index = index
            depth += 1
        elif token.value == "}":
            depth -= 1
            if depth == 0 and brace_index is not None:
                close_index = index
                break
    if brace_index is None:
        parts = _path_parts(tokens)
        if parts == ("self",) and prefix:
            return [prefix]
        return [prefix + parts] if parts or prefix else []

    base = prefix + _path_parts(tokens[:brace_index])
    inside = tokens[brace_index + 1 : close_index]
    expanded = []
    for group in _split_top_level(inside):
        expanded.extend(_expand_use_tree(group, base))
    return expanded


def _find_statement_end(tokens: list[_Token], start: int) -> int:
    depth = 0
    for index in range(start, len(tokens)):
        value = tokens[index].value
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
        elif value == ";" and depth == 0:
            return index
    return len(tokens)


def _scan_source(source: str) -> _RustItems:
    tokens = _tokenize(source)
    items = _RustItems()
    brace_depth = 0
    inline_stack: list[tuple[int, str]] = []
    inline_openings: dict[int, str] = {}
    pending_path: str | None = None
    index = 0

    while index < len(tokens):
        token = tokens[index]
        value = token.value
        scope = tuple(name for _depth, name in inline_stack)

        if value == "#" and index + 4 < len(tokens) and tokens[index + 1].value == "[":
            end = index + 2
            attr_depth = 1
            while end < len(tokens) and attr_depth:
                if tokens[end].value == "[":
                    attr_depth += 1
                elif tokens[end].value == "]":
                    attr_depth -= 1
                end += 1
            attribute = tokens[index + 2 : end - 1]
            for attr_index in range(len(attribute) - 2):
                if (
                    attribute[attr_index].value == "path"
                    and attribute[attr_index + 1].value == "="
                    and attribute[attr_index + 2].kind == "string"
                ):
                    pending_path = attribute[attr_index + 2].value
                    break
            index = end
            continue

        if value == "use":
            end = _find_statement_end(tokens, index + 1)
            for path in _expand_use_tree(tokens[index + 1 : end]):
                if path:
                    items.uses.append((path, scope))
            pending_path = None
            index = end + 1
            continue

        if (
            value == "extern"
            and index + 2 < len(tokens)
            and tokens[index + 1].value == "crate"
            and tokens[index + 2].kind == "identifier"
        ):
            items.extern_crates.append((tokens[index + 2].value, scope))

        if value == "mod" and index + 1 < len(tokens):
            name_token = tokens[index + 1]
            if name_token.kind == "identifier":
                next_index = index + 2
                if next_index < len(tokens) and tokens[next_index].value == ";":
                    items.mods.append(_ModDecl(name_token.value, scope, pending_path))
                elif next_index < len(tokens) and tokens[next_index].value == "{":
                    inline_openings[next_index] = name_token.value
                pending_path = None

        if value == "{":
            brace_depth += 1
            if index in inline_openings:
                inline_stack.append((brace_depth, inline_openings[index]))
            pending_path = None
        elif value == "}":
            if inline_stack and inline_stack[-1][0] == brace_depth:
                inline_stack.pop()
            brace_depth = max(0, brace_depth - 1)
            pending_path = None
        elif value == ";":
            pending_path = None

        index += 1
    return items


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _explicit_target_paths(
    data: dict, package_root: Path
) -> list[tuple[Path, str, str]]:
    targets = []
    lib = data.get("lib")
    if isinstance(lib, dict) and lib.get("path"):
        targets.append(
            (package_root / str(lib["path"]), str(lib.get("name", "")), "lib")
        )
    for table_name, kind in (
        ("bin", "bin"),
        ("example", "example"),
        ("test", "test"),
        ("bench", "bench"),
    ):
        for spec in data.get(table_name, []):
            if isinstance(spec, dict) and spec.get("path"):
                targets.append(
                    (package_root / str(spec["path"]), str(spec.get("name", "")), kind)
                )
    return targets


def _autodiscovered_targets(package: _CargoPackage) -> list[tuple[Path, str, str]]:
    data = package.data
    package_data = data.get("package", {})
    root = package.root
    targets = []
    explicit_lib_path = isinstance(data.get("lib"), dict) and data["lib"].get("path")
    if (
        package_data.get("autolib", True)
        and not explicit_lib_path
        and (root / "src/lib.rs").is_file()
    ):
        targets.append((root / "src/lib.rs", package.lib_name, "lib"))
    build_setting = package_data.get("build", "build.rs")
    if build_setting is not False:
        build_path = root / (
            build_setting if isinstance(build_setting, str) else "build.rs"
        )
        if build_path.is_file():
            targets.append((build_path, package.name, "build"))
    if package_data.get("autobins", True):
        if (root / "src/main.rs").is_file():
            targets.append((root / "src/main.rs", package.name, "bin"))
        for path in sorted((root / "src/bin").glob("*.rs")):
            targets.append((path, path.stem, "bin"))
        for path in sorted((root / "src/bin").glob("*/main.rs")):
            targets.append((path, path.parent.name, "bin"))
    for directory, flag, kind in (
        ("examples", "autoexamples", "example"),
        ("tests", "autotests", "test"),
        ("benches", "autobenches", "bench"),
    ):
        if not package_data.get(flag, True):
            continue
        for path in sorted((root / directory).glob("*.rs")):
            targets.append((path, path.stem, kind))
        for path in sorted((root / directory).glob("*/main.rs")):
            targets.append((path, path.parent.name, kind))
    return targets


def _discover_packages(
    root: Path, patterns: list
) -> tuple[list[_CargoPackage], list[tuple[Path, dict]]]:
    manifests = []
    packages = []
    for manifest in sorted(root.rglob("Cargo.toml")):
        if is_skip_dir(manifest) or is_ignored(manifest, root, patterns):
            continue
        data = _read_toml(manifest)
        manifests.append((manifest.parent, data))
        package_data = data.get("package")
        if not isinstance(package_data, dict) or not package_data.get("name"):
            continue
        name = str(package_data["name"])
        lib = data.get("lib", {}) if isinstance(data.get("lib"), dict) else {}
        lib_name = str(lib.get("name", name.replace("-", "_")))
        package = _CargoPackage(manifest.parent, data, name, lib_name)
        raw_targets = _explicit_target_paths(data, manifest.parent)
        raw_targets.extend(_autodiscovered_targets(package))
        seen = set()
        for path, target_name, kind in raw_targets:
            path = path.resolve()
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            crate_name = target_name or (lib_name if kind == "lib" else path.stem)
            package.targets.append(_CrateTarget(path, crate_name, kind, package))
        packages.append(package)
    return packages, manifests


def _dependency_specs(data: dict):
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        table = data.get(section, {})
        if isinstance(table, dict):
            yield from table.items()
    targets = data.get("target", {})
    if isinstance(targets, dict):
        for target_data in targets.values():
            if not isinstance(target_data, dict):
                continue
            for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                table = target_data.get(section, {})
                if isinstance(table, dict):
                    yield from table.items()


def _workspace_dependency(
    package: _CargoPackage, alias: str, manifests: list[tuple[Path, dict]]
) -> tuple[object, Path] | None:
    candidates = []
    for manifest_root, data in manifests:
        try:
            package.root.relative_to(manifest_root)
        except ValueError:
            continue
        workspace = data.get("workspace", {})
        dependencies = (
            workspace.get("dependencies", {}) if isinstance(workspace, dict) else {}
        )
        if alias in dependencies:
            candidates.append(
                (len(manifest_root.parts), dependencies[alias], manifest_root)
            )
    if not candidates:
        return None
    _depth, spec, manifest_root = max(candidates, key=lambda item: item[0])
    return spec, manifest_root


def _connect_local_dependencies(
    packages: list[_CargoPackage], manifests: list[tuple[Path, dict]]
) -> None:
    by_root = {package.root.resolve(): package for package in packages}
    for package in packages:
        for raw_alias, raw_spec in _dependency_specs(package.data):
            alias = str(raw_alias).replace("-", "_")
            spec = raw_spec
            spec_root = package.root
            if isinstance(spec, dict) and spec.get("workspace") is True:
                inherited = _workspace_dependency(package, str(raw_alias), manifests)
                if inherited is not None:
                    spec, spec_root = inherited
            target = None
            if isinstance(spec, dict) and spec.get("path"):
                target = by_root.get((spec_root / str(spec["path"])).resolve())
            if target is not None:
                package.local_dependencies[alias] = target


def _module_parts(path: Path, base_dir: Path) -> tuple[str, ...]:
    relative = path.relative_to(base_dir)
    if path.name == "mod.rs":
        return relative.parent.parts
    return relative.with_suffix("").parts


def _module_candidate(
    source: Path,
    declaration: _ModDecl,
    is_crate_root: bool,
    all_files: set[str],
    root: Path,
) -> str | None:
    if declaration.path_override:
        if declaration.inline_scope:
            base = (
                source.parent
                if is_crate_root or source.name == "mod.rs"
                else source.parent / source.stem
            )
            base = base.joinpath(*declaration.inline_scope)
        else:
            base = source.parent
        candidates = [base / declaration.path_override]
    else:
        base = (
            source.parent
            if is_crate_root or source.name == "mod.rs"
            else source.parent / source.stem
        )
        base = base.joinpath(*declaration.inline_scope)
        candidates = [
            base / f"{declaration.name}.rs",
            base / declaration.name / "mod.rs",
        ]
    for candidate in candidates:
        try:
            relative = _relative(candidate.resolve(), root)
        except ValueError:
            continue
        if relative in all_files:
            return relative
    return None


def _add_module(
    target: _CrateTarget,
    module: tuple[str, ...],
    relative: str,
    *,
    declared: bool = False,
) -> bool:
    if declared:
        declared_candidates = target.declared_modules.setdefault(module, [])
        if relative not in declared_candidates:
            declared_candidates.append(relative)
    candidates = target.modules.setdefault(module, [])
    if relative in candidates:
        return False
    candidates.append(relative)
    paths = target.file_modules.setdefault(relative, [])
    if module not in paths:
        paths.append(module)
    return True


def _build_target_modules(
    target: _CrateTarget,
    package_files: list[Path],
    other_targets: list[_CrateTarget],
    all_files: set[str],
    root: Path,
    source_items: dict[str, _RustItems],
) -> None:
    if target.root_file is None:
        base_dir = target.package.root if target.package else root
    else:
        base_dir = target.root_file.parent
        _add_module(target, (), _relative(target.root_file, root))

    excluded_roots = {
        other.root_file.resolve()
        for other in other_targets
        if other is not target and other.root_file is not None
    }
    excluded_dirs = {
        other.root_file.parent.resolve()
        for other in other_targets
        if other is not target
        and other.root_file is not None
        and other.root_file.name == "main.rs"
        and other.root_file.parent != base_dir
    }
    for path in package_files:
        resolved = path.resolve()
        if target.root_file is not None and resolved == target.root_file.resolve():
            continue
        if resolved in excluded_roots:
            continue
        if any(directory in resolved.parents for directory in excluded_dirs):
            continue
        try:
            module = _module_parts(path, base_dir)
        except ValueError:
            continue
        _add_module(target, module, _relative(path, root))

    pending = deque(
        (relative, module)
        for relative, modules in target.file_modules.items()
        for module in modules
    )
    while pending:
        relative, source_module = pending.popleft()
        source = root / relative
        items = source_items.get(relative, _RustItems())
        for declaration in items.mods:
            candidate = _module_candidate(
                source,
                declaration,
                target.root_file is not None
                and source.resolve() == target.root_file.resolve(),
                all_files,
                root,
            )
            if candidate is None:
                continue
            logical = source_module + declaration.inline_scope + (declaration.name,)
            if _add_module(target, logical, candidate, declared=True):
                pending.append((candidate, logical))


def _discover_targets(
    root: Path,
    files: list[Path],
    patterns: list,
    source_items: dict[str, _RustItems],
) -> list[_CrateTarget]:
    packages, manifests = _discover_packages(root, patterns)
    _connect_local_dependencies(packages, manifests)
    targets = [target for package in packages for target in package.targets]

    if not targets:
        roots = [path for path in files if path.name in {"lib.rs", "main.rs"}]
        if roots:
            targets = [_CrateTarget(path, path.stem, "loose", None) for path in roots]
        else:
            targets = [_CrateTarget(None, "", "loose", None)]

    package_roots = sorted(
        (package.root for package in packages),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    package_files: dict[Path | None, list[Path]] = {
        package.root: [] for package in packages
    }
    package_files[None] = []
    for path in files:
        owner = next(
            (candidate for candidate in package_roots if candidate in path.parents),
            None,
        )
        package_files.setdefault(owner, []).append(path)

    all_files = {_relative(path, root) for path in files}
    for target in targets:
        owner = target.package.root if target.package else None
        siblings = target.package.targets if target.package else targets
        _build_target_modules(
            target,
            package_files.get(owner, files),
            siblings,
            all_files,
            root,
            source_items,
        )
    return targets


def _longest_modules(
    target: _CrateTarget, parts: tuple[str, ...], minimum: int
) -> list[str]:
    for length in range(len(parts), minimum - 1, -1):
        prefix = parts[:length]
        # Explicit declarations replace filesystem guesses, but retain every
        # statically visible conditional variant at the same logical path.
        candidates = target.declared_modules.get(prefix) or target.modules.get(prefix)
        if candidates:
            return candidates
    return []


def _library_target(package: _CargoPackage) -> _CrateTarget | None:
    return next((target for target in package.targets if target.kind == "lib"), None)


def _resolve_use(
    parts: tuple[str, ...],
    source_module: tuple[str, ...],
    target: _CrateTarget,
) -> list[tuple[str, str]]:
    if not parts:
        return []
    first = parts[0]
    if first == "crate":
        return [("internal", path) for path in _longest_modules(target, parts[1:], 0)]
    if first in {"self", "super"}:
        base = list(source_module)
        index = 0
        if first == "self":
            index = 1
        else:
            while index < len(parts) and parts[index] == "super":
                if base:
                    base.pop()
                index += 1
        return [
            ("internal", path)
            for path in _longest_modules(target, tuple(base) + parts[index:], 0)
        ]

    for base in (source_module, ()):
        resolved = _longest_modules(target, base + parts, len(base) + 1)
        if resolved:
            return [("internal", path) for path in resolved]

    package = target.package
    dependency = package.local_dependencies.get(first) if package else None
    if (
        dependency is None
        and package
        and first == package.lib_name
        and target.kind != "lib"
    ):
        dependency = package
    if dependency is not None:
        library = _library_target(dependency)
        if library is not None:
            resolved = _longest_modules(library, parts[1:], 0)
            if resolved:
                return [("internal", path) for path in resolved]

    if first not in STDLIB_CRATES:
        return [("external", first)]
    return []


def resolve_mod_decl(mod_name: str, file_path: Path, root: Path, all_files: set):
    """Resolve a conventional outlined module declaration."""
    declaration = _ModDecl(mod_name, ())
    is_root = file_path.name in {"lib.rs", "main.rs"}
    return _module_candidate(file_path, declaration, is_root, all_files, root)


def resolve_use_internal(use_path: str, root: Path, all_files: set):
    """Compatibility helper for resolving simple crate-relative paths."""
    if not use_path.startswith("crate::"):
        return None
    parts = tuple(use_path[7:].split("::"))
    for length in range(len(parts), 0, -1):
        relative = "/".join(parts[:length])
        for prefix in ("src", ""):
            base = f"{prefix}/{relative}" if prefix else relative
            for candidate in (base + ".rs", base + "/mod.rs"):
                if candidate in all_files:
                    return candidate
    return None


def analyze(root: Path, group_map: dict):
    patterns = load_gitignore_patterns(root)
    rs_files = collect_files(root, patterns)

    if not rs_files:
        return [], [], {}, {"total_files": 0, "total_loc": 0}

    sources = {}
    source_items = {}
    total_loc = 0
    for path in rs_files:
        relative = _relative(path, root)
        try:
            source = path.read_text(errors="replace")
        except OSError:
            continue
        sources[relative] = source
        source_items[relative] = _scan_source(source)
        total_loc += source.count("\n") + 1

    targets = _discover_targets(root, rs_files, patterns, source_items)
    all_files = set(sources)
    targets_by_file: dict[str, list[_CrateTarget]] = {}
    for target in targets:
        for relative in target.file_modules:
            targets_by_file.setdefault(relative, []).append(target)

    nodes = []
    links_map = {}
    external_nodes = {}
    for path in rs_files:
        relative = _relative(path, root)
        source = sources.get(relative)
        if source is None:
            continue
        items = source_items[relative]
        import_targets = []
        contexts = targets_by_file.get(relative, [])

        for declaration in items.mods:
            candidates = contexts or [None]
            for context in candidates:
                is_root = (
                    context is not None
                    and context.root_file is not None
                    and path.resolve() == context.root_file.resolve()
                )
                target_path = _module_candidate(
                    path, declaration, is_root, all_files, root
                )
                if target_path:
                    import_targets.append(("internal", target_path))

        for context in contexts:
            source_modules = context.file_modules.get(relative, [()])
            for use_path, inline_scope in items.uses:
                for source_module in source_modules:
                    resolved = _resolve_use(
                        use_path, source_module + inline_scope, context
                    )
                    if resolved:
                        import_targets.extend(resolved)
            for crate, inline_scope in items.extern_crates:
                for source_module in source_modules:
                    resolved = _resolve_use(
                        (crate,), source_module + inline_scope, context
                    )
                    if resolved:
                        import_targets.extend(resolved)

        seen = set()
        deduped = []
        for item in import_targets:
            if item not in seen and not (item[0] == "internal" and item[1] == relative):
                seen.add(item)
                deduped.append(item)

        loc = source.count("\n") + 1
        nodes.append(
            {
                "id": relative,
                "type": node_type(path),
                "language": "rust",
                "size": loc,
                "loc": loc,
                "group": dir_group(path, root, group_map),
                "imports": len(deduped),
            }
        )

        for kind, target_path in deduped:
            if kind == "internal":
                key = (relative, target_path)
            else:
                if target_path not in external_nodes:
                    external_nodes[target_path] = {
                        "id": target_path,
                        "type": "import",
                        "language": "rust",
                        "size": 40,
                        "loc": 0,
                        "group": 9000,
                        "imports": 0,
                    }
                key = (relative, target_path)
            links_map[key] = links_map.get(key, 0) + 1

    return (
        nodes,
        list(external_nodes.values()),
        links_map,
        {"total_files": len(rs_files), "total_loc": total_loc},
    )

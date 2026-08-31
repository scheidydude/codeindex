# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

"""Per-language symbol extraction for the blastradius symbol index."""

import ast
import re
from pathlib import Path


def _line_of(source: str, pos: int) -> int:
    return source[:pos].count("\n") + 1


# ── Python ────────────────────────────────────────────────────────────────────


def extract_python(path: Path) -> list[dict]:
    try:
        # Parsing bytes honors Python source encodings, not the host locale.
        source = path.read_bytes()
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return []

    symbols = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node) or ""
            sym: dict = {
                "name": node.name,
                "line": node.lineno,
                "kind": "function",
                "exported": not node.name.startswith("_"),
            }
            if doc:
                sym["doc"] = doc.split("\n")[0][:80]
            symbols.append(sym)
        elif isinstance(node, ast.ClassDef):
            methods = [
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not child.name.startswith("__")
            ]
            doc = ast.get_docstring(node) or ""
            sym = {
                "name": node.name,
                "line": node.lineno,
                "kind": "class",
                "exported": not node.name.startswith("_"),
            }
            if methods:
                sym["methods"] = methods
            if doc:
                sym["doc"] = doc.split("\n")[0][:80]
            symbols.append(sym)
    return symbols


# ── JavaScript / TypeScript / Vue ─────────────────────────────────────────────

_JS_EXPORT_FUNC = re.compile(
    r"^export\s+(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)", re.MULTILINE
)
_JS_EXPORT_CLASS = re.compile(
    r"^export\s+(?:default\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE
)
_JS_EXPORT_CONST = re.compile(r"^export\s+(?:const|let|var)\s+(\w+)", re.MULTILINE)
_JS_EXPORT_TYPE = re.compile(r"^export\s+(?:type|interface)\s+(\w+)", re.MULTILINE)
_JS_EXPORT_ENUM = re.compile(r"^export\s+(?:const\s+)?enum\s+(\w+)", re.MULTILINE)
# export const { a, b, c } = expr()  — destructured assignment re-exports
_JS_EXPORT_DESTRUCT = re.compile(
    r"^export\s+(?:const|let|var)\s*\{([^}]+)\}", re.MULTILINE
)
# export { a, b as c } [from '...']  — named (re-)exports
_JS_EXPORT_NAMED = re.compile(r"^export\s*\{([^}]+)\}", re.MULTILINE)


def _parse_destruct_names(capture: str) -> list[str]:
    """Extract identifiers from a destructured export brace list.

    Handles: { a, b, c as d }  →  [a, b, d]
    Skips spread operators and anything that isn't a plain identifier.
    """
    names = []
    for part in capture.split(","):
        part = part.strip().lstrip(".")  # strip leading ... for spreads
        if not part:
            continue
        # "local as exported" — the exported name is the alias
        if " as " in part:
            alias = part.split(" as ")[-1].strip()
            if re.match(r"^\w+$", alias) and alias != "default":
                names.append(alias)
        else:
            name = part.split()[0] if part.split() else ""
            if name and re.match(r"^\w+$", name) and name != "default":
                names.append(name)
    return names


def extract_js(path: Path) -> list[dict]:
    try:
        source = path.read_text(errors="replace")
    except OSError:
        return []

    symbols = []
    seen: set[str] = set()

    for pat, kind in (
        (_JS_EXPORT_FUNC, "function"),
        (_JS_EXPORT_CLASS, "class"),
        (_JS_EXPORT_ENUM, "enum"),
        (_JS_EXPORT_TYPE, "type"),
        (_JS_EXPORT_CONST, "const"),
    ):
        for m in pat.finditer(source):
            name = m.group(1)
            if name and name not in seen:
                seen.add(name)
                symbols.append(
                    {
                        "name": name,
                        "line": _line_of(source, m.start()),
                        "kind": kind,
                        "exported": True,
                    }
                )

    # Destructured exports: export const { a, b } = SomeCall()
    for m in _JS_EXPORT_DESTRUCT.finditer(source):
        line = _line_of(source, m.start())
        for name in _parse_destruct_names(m.group(1)):
            if name not in seen:
                seen.add(name)
                symbols.append(
                    {
                        "name": name,
                        "line": line,
                        "kind": "const",
                        "exported": True,
                    }
                )

    # Named (re-)exports: export { foo, bar as baz } [from '...']
    for m in _JS_EXPORT_NAMED.finditer(source):
        line = _line_of(source, m.start())
        for name in _parse_destruct_names(m.group(1)):
            if name not in seen:
                seen.add(name)
                symbols.append(
                    {
                        "name": name,
                        "line": line,
                        "kind": "const",
                        "exported": True,
                    }
                )

    return symbols


# ── Go ────────────────────────────────────────────────────────────────────────

_GO_FUNC = re.compile(r"^func\s+(?:\([^)]+\)\s+)?(\w+)\s*[\[(]", re.MULTILINE)
_GO_TYPE_STRUCT = re.compile(r"^type\s+(\w+)\s+struct\b", re.MULTILINE)
_GO_TYPE_IFACE = re.compile(r"^type\s+(\w+)\s+interface\b", re.MULTILINE)


def extract_go(path: Path) -> list[dict]:
    try:
        source = path.read_text(errors="replace")
    except OSError:
        return []

    symbols = []
    seen: set[str] = set()

    for m in _GO_FUNC.finditer(source):
        name = m.group(1)
        if name and name not in seen:
            seen.add(name)
            symbols.append(
                {
                    "name": name,
                    "line": _line_of(source, m.start()),
                    "kind": "function",
                    "exported": name[0].isupper(),
                }
            )

    for pat, kind in ((_GO_TYPE_STRUCT, "struct"), (_GO_TYPE_IFACE, "interface")):
        for m in pat.finditer(source):
            name = m.group(1)
            if name and name not in seen:
                seen.add(name)
                symbols.append(
                    {
                        "name": name,
                        "line": _line_of(source, m.start()),
                        "kind": kind,
                        "exported": name[0].isupper(),
                    }
                )

    return symbols


# ── Java / Kotlin ─────────────────────────────────────────────────────────────

_JAVA_TYPE = re.compile(
    r"(?:^|\n)\s*(?:public\s+|private\s+|protected\s+)?(?:abstract\s+|final\s+|sealed\s+)?"
    r"(?:class|interface|enum|record|@interface)\s+(\w+)",
    re.MULTILINE,
)
_JAVA_METHOD = re.compile(
    r"(?:public|private|protected)\s+"
    r"(?:(?:static|final|abstract|synchronized|native|default)\s+)*"
    r"(?:[\w<>\[\]]+\s+)+(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w,\s]+)?\s*[{;]",
    re.MULTILINE,
)
_KOTLIN_CLASS = re.compile(
    r"^(?:\s*)(?:(?:public|private|protected|internal|open|abstract|sealed|data|enum|annotation|inner|value)\s+)*"
    r"(?:class|interface|object)\s+(\w+)",
    re.MULTILINE,
)
_KOTLIN_FUN = re.compile(
    r"^(?:\s*)(?:(?:public|private|protected|internal|override|suspend|inline|open|abstract|operator|infix|tailrec|external|actual|expect)\s+)*"
    r"fun\s+(?:<[^>]+>\s+)?(\w+)\s*[\(<]",
    re.MULTILINE,
)

_JAVA_NOISE = {
    "if",
    "for",
    "while",
    "return",
    "new",
    "throw",
    "catch",
    "switch",
    "case",
}


def extract_java(path: Path) -> list[dict]:
    try:
        source = path.read_text(errors="replace")
    except OSError:
        return []

    is_kotlin = path.suffix.lower() in {".kt", ".kts"}
    symbols = []
    seen: set[str] = set()

    type_pat = _KOTLIN_CLASS if is_kotlin else _JAVA_TYPE
    method_pat = _KOTLIN_FUN if is_kotlin else _JAVA_METHOD

    for m in type_pat.finditer(source):
        name = m.group(1)
        if name and name not in seen:
            seen.add(name)
            symbols.append(
                {
                    "name": name,
                    "line": _line_of(source, m.start()),
                    "kind": "class",
                    "exported": True,
                }
            )

    for m in method_pat.finditer(source):
        name = m.group(1)
        if name and name not in seen and name not in _JAVA_NOISE:
            seen.add(name)
            symbols.append(
                {
                    "name": name,
                    "line": _line_of(source, m.start()),
                    "kind": "function",
                    "exported": True,
                }
            )

    return symbols


# ── Rust ─────────────────────────────────────────────────────────────────────

_RUST_FN = re.compile(
    r"^(?:pub(?:\s*\([^)]+\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)", re.MULTILINE
)
_RUST_STRUCT = re.compile(r"^(?:pub(?:\s*\([^)]+\))?\s+)?struct\s+(\w+)", re.MULTILINE)
_RUST_ENUM = re.compile(r"^(?:pub(?:\s*\([^)]+\))?\s+)?enum\s+(\w+)", re.MULTILINE)
_RUST_TRAIT = re.compile(r"^(?:pub(?:\s*\([^)]+\))?\s+)?trait\s+(\w+)", re.MULTILINE)


def extract_rust(path: Path) -> list[dict]:
    try:
        source = path.read_text(errors="replace")
    except OSError:
        return []

    symbols = []
    seen: set[str] = set()

    for pat, kind in (
        (_RUST_FN, "function"),
        (_RUST_STRUCT, "struct"),
        (_RUST_ENUM, "enum"),
        (_RUST_TRAIT, "trait"),
    ):
        for m in pat.finditer(source):
            name = m.group(1)
            if name and name not in seen:
                seen.add(name)
                symbols.append(
                    {
                        "name": name,
                        "line": _line_of(source, m.start()),
                        "kind": kind,
                        "exported": m.group(0).startswith("pub"),
                    }
                )

    return symbols


# ── PHP ───────────────────────────────────────────────────────────────────────

_PHP_CLASS = re.compile(r"^(?:abstract\s+|final\s+)?class\s+(\w+)", re.MULTILINE)
_PHP_IFACE = re.compile(r"^interface\s+(\w+)", re.MULTILINE)
_PHP_FUNC = re.compile(r"^function\s+(\w+)\s*\(", re.MULTILINE)


def extract_php(path: Path) -> list[dict]:
    try:
        source = path.read_text(errors="replace")
    except OSError:
        return []

    symbols = []
    seen: set[str] = set()

    for pat, kind in (
        (_PHP_CLASS, "class"),
        (_PHP_IFACE, "interface"),
        (_PHP_FUNC, "function"),
    ):
        for m in pat.finditer(source):
            name = m.group(1)
            if name and name not in seen:
                seen.add(name)
                symbols.append(
                    {
                        "name": name,
                        "line": _line_of(source, m.start()),
                        "kind": kind,
                        "exported": True,
                    }
                )

    return symbols


# ── Ruby ─────────────────────────────────────────────────────────────────────

_RUBY_CLASS = re.compile(r"^class\s+([A-Z]\w*)", re.MULTILINE)
_RUBY_MODULE = re.compile(r"^module\s+([A-Z]\w*)", re.MULTILINE)
_RUBY_DEF = re.compile(r"^\s*def\s+(?:self\.)?(\w+[?!]?)", re.MULTILINE)


def extract_ruby(path: Path) -> list[dict]:
    try:
        source = path.read_text(errors="replace")
    except OSError:
        return []

    symbols = []
    seen: set[str] = set()

    for pat, kind in (
        (_RUBY_CLASS, "class"),
        (_RUBY_MODULE, "module"),
        (_RUBY_DEF, "function"),
    ):
        for m in pat.finditer(source):
            name = m.group(1)
            if name and name not in seen:
                seen.add(name)
                symbols.append(
                    {
                        "name": name,
                        "line": _line_of(source, m.start()),
                        "kind": kind,
                        "exported": True,
                    }
                )

    return symbols


# ── Terraform / HCL ──────────────────────────────────────────────────────────

_TF_RESOURCE_RE = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE)
_TF_DATA_RE = re.compile(r'^data\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE)
_TF_MODULE_RE = re.compile(r'^module\s+"([^"]+)"', re.MULTILINE)
_TF_VAR_RE = re.compile(r'^variable\s+"([^"]+)"', re.MULTILINE)
_TF_OUTPUT_RE = re.compile(r'^output\s+"([^"]+)"', re.MULTILINE)
_TF_PROVIDER_RE = re.compile(r'^provider\s+"([^"]+)"', re.MULTILINE)


def extract_terraform(path: Path) -> list[dict]:
    try:
        source = path.read_text(errors="replace")
    except OSError:
        return []

    symbols: list[dict] = []
    seen: set[str] = set()

    for pat, kind, fmt, exported in (
        (_TF_RESOURCE_RE, "resource", "{0}.{1}", True),
        (_TF_DATA_RE, "data", "data.{0}.{1}", True),
        (_TF_MODULE_RE, "module", "module.{0}", True),
        (_TF_VAR_RE, "variable", "var.{0}", True),
        (_TF_OUTPUT_RE, "output", "{0}", True),
        (_TF_PROVIDER_RE, "provider", "provider.{0}", False),
    ):
        for m in pat.finditer(source):
            name = fmt.format(*m.groups())
            if name and name not in seen:
                seen.add(name)
                symbols.append(
                    {
                        "name": name,
                        "line": _line_of(source, m.start()),
                        "kind": kind,
                        "exported": exported,
                    }
                )

    return symbols


# ── Dispatch ─────────────────────────────────────────────────────────────────

EXTRACTORS: dict[str, callable] = {
    ".py": extract_python,
    ".js": extract_js,
    ".jsx": extract_js,
    ".ts": extract_js,
    ".tsx": extract_js,
    ".mjs": extract_js,
    ".cjs": extract_js,
    ".vue": extract_js,
    ".go": extract_go,
    ".java": extract_java,
    ".kt": extract_java,
    ".kts": extract_java,
    ".rs": extract_rust,
    ".php": extract_php,
    ".rb": extract_ruby,
    ".tf": extract_terraform,
}


def extract_symbols(path: Path) -> list[dict]:
    fn = EXTRACTORS.get(path.suffix.lower())
    return fn(path) if fn else []

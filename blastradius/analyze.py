# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

"""Dispatcher: detect languages, delegate to per-language analyzers."""

import sys
from pathlib import Path

from blastradius.analyzers import (
    ci_analyzer,
    css_analyzer,
    docker_analyzer,
    go_analyzer,
    java_analyzer,
    js_analyzer,
    php_analyzer,
    python_analyzer,
    ruby_analyzer,
    rust_analyzer,
    schema_analyzer,
    terraform_analyzer,
)
from blastradius.analyzers.cross_lang_analyzer import find_api_boundaries
from blastradius.analyzers.monorepo_analyzer import assign_packages, detect_workspaces

_SKIP = {
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".git",
    "node_modules",
    "dist",
    "build",
    ".next",
    "target",
    "vendor",
    ".bundle",
}


def _any_match(root: Path, globs: list) -> bool:
    return any(
        p
        for g in globs
        for p in root.rglob(g)
        if not any(part in _SKIP for part in p.parts)
    )


def detect_languages(root: Path) -> list:
    langs = []
    if _any_match(root, ["*.py"]):
        langs.append("python")
    if (root / "package.json").exists() or _any_match(
        root, ["*.js", "*.ts", "*.jsx", "*.tsx", "*.mjs", "*.vue"]
    ):
        langs.append("javascript")
    if _any_match(root, ["*.css", "*.scss", "*.sass", "*.less", "*.styl"]):
        langs.append("css")
    if (root / "go.mod").exists() or _any_match(root, ["*.go"]):
        langs.append("go")
    if (root / "Gemfile").exists() or _any_match(root, ["*.rb"]):
        langs.append("ruby")
    if (root / "Cargo.toml").exists() or _any_match(root, ["*.rs"]):
        langs.append("rust")
    if _any_match(root, ["*.java", "*.kt", "*.kts"]):
        langs.append("java")
    if (root / "composer.json").exists() or _any_match(root, ["*.php"]):
        langs.append("php")
    compose_names = [
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    ]
    if any((root / n).exists() for n in compose_names) or _any_match(
        root, ["Dockerfile*"]
    ):
        langs.append("docker")
    if (
        (root / ".github" / "workflows").exists()
        or (root / ".gitlab-ci.yml").exists()
        or (root / ".gitlab-ci.yaml").exists()
    ):
        langs.append("ci")
    if _any_match(root, ["*.sql", "*.prisma"]):
        langs.append("schema")
    if _any_match(root, ["*.tf"]):
        langs.append("terraform")
    return langs


def merge_links(target: dict, source: dict) -> None:
    for k, v in source.items():
        target[k] = target.get(k, 0) + v


_INFRA_TYPES = {"service", "pipeline", "database"}
_FRONTEND_LANGS = {"javascript", "typescript", "vue", "css", "scss", "less", "sass"}
_BACKEND_LANGS = {"python", "go", "ruby", "rust", "java", "kotlin", "php"}
_INFRA_LANGS = {"docker", "github-actions", "gitlab-ci", "sql", "prisma", "terraform"}

_ANALYZERS = {
    "python": python_analyzer,
    "javascript": js_analyzer,
    "css": css_analyzer,
    "go": go_analyzer,
    "ruby": ruby_analyzer,
    "rust": rust_analyzer,
    "java": java_analyzer,
    "php": php_analyzer,
    "docker": docker_analyzer,
    "ci": ci_analyzer,
    "schema": schema_analyzer,
    "terraform": terraform_analyzer,
}


def assign_layer(node: dict) -> str:
    ntype = node.get("type", "module")
    if ntype in _INFRA_TYPES:
        return "infrastructure"
    lang = node.get("language", "")
    if lang in _FRONTEND_LANGS:
        return "frontend"
    if lang in _BACKEND_LANGS:
        return "backend"
    if lang in _INFRA_LANGS:
        return "infrastructure"
    return "backend"


def link_kind(s_type: str, t_type: str) -> str:
    if s_type == "style" or t_type == "style":
        return "styles"
    if s_type in _INFRA_TYPES or t_type in _INFRA_TYPES:
        return "depends"
    if s_type in {"component", "route"} and t_type in {"component", "route"}:
        return "renders"
    return "imports"


def analyze(root_path: str) -> dict:
    root = Path(root_path).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Path not found: {root}")

    langs = detect_languages(root)
    if not langs:
        print(f"Warning: no supported languages detected in {root}", file=sys.stderr)

    group_map = {}
    all_nodes = []
    all_links = {}
    ext_seen = set()
    total_files = 0
    total_loc = 0
    meta_extra = {}

    def add_results(nodes, ext_nodes, links_map, meta):
        nonlocal total_files, total_loc
        # Older analyzers return native separators. Keep graph and symbol IDs
        # portable, including links to files handled by another analyzer.
        external_ids = {node["id"] for node in ext_nodes}

        def portable_id(value):
            return value if value in external_ids else value.replace("\\", "/")

        for node in nodes:
            node["id"] = portable_id(node["id"])
        all_nodes.extend(nodes)
        for en in ext_nodes:
            if en["id"] not in ext_seen:
                all_nodes.append(en)
                ext_seen.add(en["id"])
        for (source, target), weight in links_map.items():
            key = (portable_id(source), portable_id(target))
            all_links[key] = all_links.get(key, 0) + weight
        total_files += meta.get("total_files", 0)
        total_loc += meta.get("total_loc", 0)
        for key in ("framework", "packageManager"):
            if meta.get(key):
                meta_extra.setdefault(key, meta[key])

    for lang in langs:
        analyzer = _ANALYZERS.get(lang)
        if analyzer:
            try:
                add_results(*analyzer.analyze(root, group_map))
            except Exception as e:  # noqa: BLE001
                print(f"Warning: {lang} analyzer failed: {e}", file=sys.stderr)

    for node in all_nodes:
        node.setdefault("layer", assign_layer(node))

    try:
        workspaces = {
            path.replace("\\", "/"): name
            for path, name in detect_workspaces(root).items()
        }
        assign_packages(all_nodes, workspaces)
        if workspaces:
            meta_extra["workspaces"] = list(workspaces.values())
    except Exception as e:  # noqa: BLE001
        print(f"Warning: monorepo detection failed: {e}", file=sys.stderr)

    node_type_map = {n["id"]: n.get("type", "module") for n in all_nodes}
    links = [
        {
            "source": s,
            "target": t,
            "weight": w,
            "kind": link_kind(
                node_type_map.get(s, "module"), node_type_map.get(t, "module")
            ),
        }
        for (s, t), w in all_links.items()
    ]

    try:
        api_links = find_api_boundaries(root, all_nodes)
        links.extend(api_links)
        if api_links:
            meta_extra["apiLinks"] = len(api_links)
    except Exception as e:  # noqa: BLE001
        print(f"Warning: cross-language analysis failed: {e}", file=sys.stderr)

    return {
        "meta": {
            "root": str(root.name) + "/",
            "total_files": total_files,
            "total_loc": total_loc,
            "languages": langs,
            **meta_extra,
        },
        "nodes": all_nodes,
        "links": links,
    }

# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

import pytest

from blastradius.analyze import analyze
from blastradius.artifacts import Artifacts
from blastradius.index import build
from blastradius.symbols import build_symbol_index


@pytest.fixture
def windows_relative_paths(monkeypatch, tmp_path):
    """Exercise Windows relative-path serialization on every test platform."""
    relative_to = Path.relative_to

    def windows_relative_to(self, *args, **kwargs):
        relative = relative_to(self, *args, **kwargs)
        if self == tmp_path or tmp_path in self.parents:
            return PureWindowsPath(relative.as_posix())
        return relative

    monkeypatch.setattr(Path, "relative_to", windows_relative_to)


@pytest.fixture
def legacy_text_encoding(monkeypatch):
    """Simulate cp1252 source-file reads without changing protocol encoding."""
    path_open = Path.open

    def open_with_legacy_default(
        self, mode="r", buffering=-1, encoding=None, errors=None, newline=None
    ):
        if "b" not in mode and encoding in (None, "locale"):
            encoding = "cp1252"
        return path_open(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", open_with_legacy_default)


def test_windows_export_keeps_conflicting_absolute_paths_ambiguous(
    tmp_path, windows_relative_paths
):
    (tmp_path / "models.py").write_text("class Original: pass\n", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "models.py").write_text("class Other: pass\n", encoding="utf-8")
    graph = nested / "graph.json"
    build(str(tmp_path), output=graph)

    with (
        Artifacts(tmp_path, discover=False) as artifacts,
        pytest.raises(ValueError, match="Ambiguous"),
    ):
        artifacts.index_for_file(str(nested / "models.py"), graph)


@pytest.mark.parametrize(
    ("encoding", "header"),
    [("utf-8", ""), ("utf-8-sig", ""), ("cp1252", "# coding: cp1252\n")],
)
def test_unicode_symbol_survives_non_utf8_file_defaults(
    tmp_path, legacy_text_encoding, encoding, header
):
    (tmp_path / "café.py").write_bytes((header + "class Café: pass\n").encode(encoding))
    symbols = build_symbol_index(str(tmp_path))
    assert "Café" in symbols["symbols"]
    assert symbols["symbols"]["Café"][0]["file"] == "café.py"


@pytest.mark.parametrize(
    ("encoding", "header"),
    [("utf-8", ""), ("utf-8-sig", ""), ("cp1252", "# coding: cp1252\n")],
)
def test_python_imports_survive_non_utf8_file_defaults(
    tmp_path, legacy_text_encoding, encoding, header
):
    (tmp_path / "café.py").write_text("class Café: pass\n", encoding="utf-8")
    (tmp_path / "main.py").write_bytes(
        (header + "from café import Café\n").encode(encoding)
    )
    data = analyze(str(tmp_path))
    assert {(edge["source"], edge["target"]) for edge in data["links"]} == {
        ("main.py", "café.py"),
    }


def test_windows_index_requires_unique_short_paths(tmp_path, windows_relative_paths):
    for directory in ("a", "b"):
        target = tmp_path / directory
        target.mkdir()
        (target / "same.py").write_text("class Example: pass\n", encoding="utf-8")
    build(str(tmp_path))
    with Artifacts(tmp_path, discover=False) as artifacts:
        with pytest.raises(ValueError, match="Ambiguous"):
            artifacts.index_for_file("same.py")
        for file_path in ("a/same.py", r"a\same.py", "./a/same.py"):
            _, file_id = artifacts.index_for_file(file_path)
            assert file_id == "a/same.py"
    symbols = build_symbol_index(str(tmp_path))
    assert set(symbols["file_symbols"]) == {"a/same.py", "b/same.py"}


def test_windows_python_imports_use_portable_file_ids(tmp_path, windows_relative_paths):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "models.py").write_text("class User: pass\n", encoding="utf-8")
    (package / "client.py").write_text(
        "from .models import User\nfrom . import models\nimport models\n",
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text("from pkg.models import User\n", encoding="utf-8")
    data = analyze(str(tmp_path))
    assert {(edge["source"], edge["target"]) for edge in data["links"]} == {
        ("pkg/client.py", "pkg/models.py"),
        ("main.py", "pkg/models.py"),
    }


def test_windows_mixed_language_graph_and_symbols_share_file_ids(
    tmp_path, windows_relative_paths
):
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (tmp_path / "package.json").write_text(
        json.dumps({"workspaces": ["frontend"]}), encoding="utf-8"
    )
    (frontend / "package.json").write_text(
        json.dumps({"name": "demo-web"}), encoding="utf-8"
    )
    (backend / "api.py").write_text(
        '@app.get("/api/users")\ndef users(): return []\n', encoding="utf-8"
    )
    (frontend / "client.js").write_text(
        'import { helper } from "./helper.js";\n'
        'export function loadUsers() { return fetch("/api/users"); }\n',
        encoding="utf-8",
    )
    (frontend / "helper.js").write_text("export const helper = 1;\n", encoding="utf-8")
    data = build(str(tmp_path))
    symbols = build_symbol_index(str(tmp_path))
    node_ids = {node["id"] for node in data["nodes"]}
    assert set(symbols["file_symbols"]) <= node_ids
    assert all(
        node.get("package") == "demo-web"
        for node in data["nodes"]
        if node["id"].startswith("frontend/")
    )
    assert {(edge["source"], edge["target"]) for edge in data["links"]} == {
        ("frontend/client.js", "frontend/helper.js"),
        ("frontend/client.js", "backend/api.py"),
    }


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_python_binary_reads_preserve_line_counts(tmp_path, newline):
    (tmp_path / "main.py").write_bytes(newline.join([b"import os", b"value = 1", b""]))
    data = analyze(str(tmp_path))
    node = next(node for node in data["nodes"] if node["id"] == "main.py")
    assert node["loc"] == 3
    assert node["imports"] == 1


@pytest.mark.parametrize(
    "source",
    [b"# coding: unknown-encoding\nclass Bad: pass\n", b"class Bad\xff: pass\n"],
)
def test_invalid_python_encoding_does_not_abort_analysis(tmp_path, source):
    (tmp_path / "bad.py").write_bytes(source)
    (tmp_path / "good.py").write_text("class Good: pass\n", encoding="utf-8")
    data = analyze(str(tmp_path))
    assert {node["id"] for node in data["nodes"]} == {"bad.py", "good.py"}
    symbols = build_symbol_index(str(tmp_path))
    assert set(symbols["symbols"]) == {"Good"}

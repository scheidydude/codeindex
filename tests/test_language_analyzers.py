# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from blastradius.analyze import analyze


def _write(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _links(root: Path) -> set[tuple[str, str]]:
    data = analyze(str(root))
    return {(link["source"], link["target"]) for link in data["links"]}


def _bounded_analysis(root: Path) -> dict:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; from blastradius.analyze import analyze; "
                "print(json.dumps(analyze(sys.argv[1])))"
            ),
            str(root),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def test_rust_conditional_module_discovery_terminates(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.0"\n')
    _write(
        tmp_path,
        "src/lib.rs",
        '#[cfg(unix)]\n#[path = "unix.rs"]\nmod platform;\n'
        '#[cfg(windows)]\n#[path = "windows.rs"]\nmod platform;\n',
    )
    _write(tmp_path, "src/unix.rs", "pub fn run() {}\n")
    _write(tmp_path, "src/windows.rs", "pub fn run() {}\n")

    data = _bounded_analysis(tmp_path)
    assert {(edge["source"], edge["target"]) for edge in data["links"]} == {
        ("src/lib.rs", "src/unix.rs"),
        ("src/lib.rs", "src/windows.rs"),
    }


def test_rust_imports_retain_both_conditional_variants_and_nested_modules(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.0"\n')
    _write(
        tmp_path,
        "src/lib.rs",
        '#[cfg(unix)]\n#[path = "unix.rs"]\nmod platform;\n'
        '#[cfg(windows)]\n#[path = "windows.rs"]\nmod platform;\n'
        "mod consumer;\n",
    )
    for variant in ("unix", "windows"):
        _write(
            tmp_path,
            f"src/{variant}.rs",
            "pub mod detail;\npub fn run() {}\nuse self::detail::Item;\n",
        )
        _write(tmp_path, f"src/{variant}/detail.rs", "pub struct Item;\n")
    _write(
        tmp_path,
        "src/consumer.rs",
        "use crate::platform::{self, run, run as alias};\n"
        "use crate::platform::detail::Item;\n"
        "use crate::platform::detail::Item as Another;\n",
    )

    data = _bounded_analysis(tmp_path)
    edges = [edge for edge in data["links"] if edge["source"] == "src/consumer.rs"]
    assert {edge["target"] for edge in edges} == {
        "src/unix.rs",
        "src/windows.rs",
        "src/unix/detail.rs",
        "src/windows/detail.rs",
    }
    assert len(edges) == 4
    assert all(edge["weight"] == 1 for edge in edges)
    consumer = next(node for node in data["nodes"] if node["id"] == "src/consumer.rs")
    assert consumer["imports"] == 4


def test_rust_declared_path_overrides_an_inferred_module_file(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.0"\n')
    _write(
        tmp_path,
        "src/lib.rs",
        '#[path = "actual.rs"]\nmod platform;\nmod consumer;\n',
    )
    _write(tmp_path, "src/actual.rs", "pub fn run() {}\n")
    _write(tmp_path, "src/platform.rs", "pub fn unused() {}\n")
    _write(tmp_path, "src/consumer.rs", "use crate::platform::run;\n")

    links = _links(tmp_path)
    assert ("src/consumer.rs", "src/actual.rs") in links
    assert ("src/consumer.rs", "src/platform.rs") not in links


def test_rust_crate_use_resolves_module_before_imported_symbol(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "Cargo.toml",
        '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n',
    )
    _write(tmp_path, "src/lib.rs", "mod api;\nmod models;\nmod service;\n")
    _write(tmp_path, "src/models.rs", "pub struct User;\n")
    _write(tmp_path, "src/api.rs", "pub mod handler;\n")
    _write(tmp_path, "src/api/handler.rs", "pub fn handle() {}\n")
    _write(
        tmp_path,
        "src/service.rs",
        "pub use crate::models::User;\nuse super::api::handler;\n",
    )

    links = _links(tmp_path)
    assert ("src/service.rs", "src/models.rs") in links
    assert ("src/service.rs", "src/api/handler.rs") in links


def test_rust_public_grouped_use_ignores_comments_and_literals(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Cargo.toml",
        '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n',
    )
    _write(tmp_path, "src/lib.rs", "mod api;\nmod models;\nmod service;\n")
    _write(tmp_path, "src/api.rs", "pub fn handler() {}\n")
    _write(tmp_path, "src/models.rs", "pub struct User;\n")
    _write(
        tmp_path,
        "src/service.rs",
        """pub use crate::{
    api::handler as handle,
    models::{self, User},
};
// use crate::ghost::Commented;
const EXAMPLE: &str = "use crate::ghost::Literal;";
/* use crate::ghost::Blocked; */
""",
    )

    links = _links(tmp_path)
    assert ("src/service.rs", "src/api.rs") in links
    assert ("src/service.rs", "src/models.rs") in links
    assert not any(
        source == "src/service.rs" and "ghost" in target for source, target in links
    )


def test_rust_relative_uses_resolve_from_current_module(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.0"\n')
    _write(tmp_path, "src/lib.rs", "mod parent;\n")
    _write(tmp_path, "src/parent/mod.rs", "mod child;\nmod sibling;\n")
    _write(
        tmp_path,
        "src/parent/child.rs",
        "mod nested;\nuse self::nested::Item;\nuse super::sibling::Item;\n",
    )
    _write(tmp_path, "src/parent/child/nested.rs", "pub struct Item;\n")
    _write(tmp_path, "src/parent/sibling.rs", "pub struct Item;\n")

    links = _links(tmp_path)
    assert ("src/parent/child.rs", "src/parent/child/nested.rs") in links
    assert ("src/parent/child.rs", "src/parent/sibling.rs") in links


def test_rust_nested_and_path_overridden_modules_follow_rust_layout(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.0"\n')
    _write(
        tmp_path,
        "src/lib.rs",
        'mod api;\n#[path = "custom/domain.rs"]\nmod domain;\n',
    )
    _write(tmp_path, "src/api.rs", "mod handler;\n")
    _write(tmp_path, "src/api/handler.rs", "pub fn handle() {}\n")
    _write(tmp_path, "src/custom/domain.rs", "pub struct Entity;\n")

    links = _links(tmp_path)
    assert ("src/api.rs", "src/api/handler.rs") in links
    assert ("src/lib.rs", "src/custom/domain.rs") in links


def test_rust_custom_library_root_anchors_crate_paths(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Cargo.toml",
        '[package]\nname = "demo"\nversion = "0.1.0"\n[lib]\npath = "code/root.rs"\n',
    )
    _write(tmp_path, "code/root.rs", "mod models;\nmod service;\n")
    _write(tmp_path, "code/models.rs", "pub struct Item;\n")
    _write(tmp_path, "code/service.rs", "use crate::models::Item;\n")

    assert ("code/service.rs", "code/models.rs") in _links(tmp_path)


def test_rust_workspace_path_dependency_links_to_local_crate(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Cargo.toml",
        '[workspace]\nmembers = ["crates/api", "crates/domain"]\nresolver = "2"\n'
        "[workspace.dependencies]\n"
        'domain = { path = "crates/domain", package = "domain-core" }\n',
    )
    _write(
        tmp_path,
        "crates/domain/Cargo.toml",
        '[package]\nname = "domain-core"\nversion = "0.1.0"\n',
    )
    _write(tmp_path, "crates/domain/src/lib.rs", "pub mod models;\n")
    _write(tmp_path, "crates/domain/src/models.rs", "pub struct User;\n")
    _write(
        tmp_path,
        "crates/api/Cargo.toml",
        '[package]\nname = "api"\nversion = "0.1.0"\n'
        "[dependencies]\ndomain.workspace = true\n",
    )
    _write(tmp_path, "crates/api/src/lib.rs", "mod service;\n")
    _write(
        tmp_path,
        "crates/api/src/service.rs",
        "use domain::models::User;\n",
    )

    assert (
        "crates/api/src/service.rs",
        "crates/domain/src/models.rs",
    ) in _links(tmp_path)


def test_rust_extern_crate_creates_external_dependency(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.0"\n')
    _write(tmp_path, "src/lib.rs", "extern crate serde_json as json;\n")

    assert ("src/lib.rs", "serde_json") in _links(tmp_path)


def test_go_import_scanner_supports_aliases_and_ignores_comments(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "go.mod",
        "module example.com/app\n\ngo 1.22\n\nrequire example.com/toolkit v1.2.3\n",
    )
    _write(
        tmp_path,
        "cmd/app/main.go",
        """package main

import (
    . "example.com/toolkit/helpers"
    _ "github.com/acme/driver/sql"
    client "github.com/acme/client/pkg"
    // _ "ghost.dev/commented/import"
)
""",
    )

    links = _links(tmp_path)
    assert ("cmd/app", "example.com/toolkit") in links
    assert ("cmd/app", "github.com/acme/driver") in links
    assert ("cmd/app", "github.com/acme/client") in links
    assert not any(target == "ghost.dev" for _source, target in links)


@pytest.mark.parametrize(
    "literals",
    [
        '"import", "github.com/ghost/dependency"',
        "`import`, `github.com/ghost/dependency`",
        '"package", "ghost", "(", ")"',
        "`import`, `(`, `github.com/ghost/dependency`, `)`",
    ],
)
def test_go_literal_keywords_do_not_create_import_edges(
    tmp_path: Path, literals: str
) -> None:
    _write(tmp_path, "go.mod", "module example.com/app\n\ngo 1.22\n")
    _write(tmp_path, "internal/driver/driver.go", "package driver\n")
    _write(
        tmp_path,
        "main.go",
        'package main\nimport (\n. "fmt"\n_ "example.com/app/internal/driver"\n)\n'
        'import osalias "os"\n'
        '// import "github.com/ghost/comment"\n'
        f"var labels = []string{{{literals}}}\n"
        "func main() { Println(osalias.Args) }\n",
    )

    data = analyze(str(tmp_path))
    assert {(edge["source"], edge["target"]) for edge in data["links"]} == {
        (".", "fmt"),
        (".", "os"),
        (".", "internal/driver"),
    }
    assert next(node for node in data["nodes"] if node["id"] == ".")["imports"] == 3


def test_go_module_boundaries_and_unresolved_imports_are_preserved(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "go.mod", "module example.com/app\n\ngo 1.22\n")
    _write(tmp_path, "internal/auth/auth.go", "package auth\n")
    _write(
        tmp_path,
        "cmd/app/main.go",
        """package main

import "example.com/app/internal/auth"
import "example.com/application/client"
import missing `example.com/app/missing`
""",
    )

    links = _links(tmp_path)
    assert ("cmd/app", "internal/auth") in links
    assert ("cmd/app", "example.com") in links
    assert ("cmd/app", "example.com/app") in links


def test_go_nested_modules_link_across_workspace_packages(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "go.work",
        "go 1.22\n\nuse (\n    ./services/api\n    ./libs/shared\n)\n",
    )
    _write(
        tmp_path,
        "services/api/go.mod",
        "module example.com/api\n\ngo 1.22\n\nrequire example.com/shared v0.0.0\n",
    )
    _write(
        tmp_path,
        "services/api/cmd/server/main.go",
        'package main\nimport "example.com/shared/client"\n',
    )
    _write(tmp_path, "libs/shared/go.mod", "module example.com/shared\n\ngo 1.22\n")
    _write(tmp_path, "libs/shared/client/client.go", "package client\n")

    assert (
        "services/api/cmd/server",
        "libs/shared/client",
    ) in _links(tmp_path)


def test_go_workspace_replace_overrides_module_local_replace(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "go.work",
        "go 1.22\n\nuse ./app\n\nreplace example.com/upstream => ./workspace-shared\n",
    )
    _write(
        tmp_path,
        "app/go.mod",
        """module example.com/app

go 1.22

require example.com/upstream v0.0.0
replace example.com/upstream => ../module-shared
""",
    )
    _write(
        tmp_path,
        "app/main.go",
        'package main\nimport "example.com/upstream/client"\n',
    )
    _write(
        tmp_path,
        "workspace-shared/go.mod",
        "module example.com/workspace-shared\n\ngo 1.22\n",
    )
    _write(tmp_path, "workspace-shared/client/client.go", "package client\n")
    _write(
        tmp_path,
        "module-shared/go.mod",
        "module example.com/module-shared\n\ngo 1.22\n",
    )
    _write(tmp_path, "module-shared/client/client.go", "package client\n")

    links = _links(tmp_path)
    assert ("app", "workspace-shared/client") in links
    assert ("app", "module-shared/client") not in links


def test_go_module_local_replace_links_to_repository_package(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app/go.mod",
        """module example.com/app

go 1.22

require example.com/upstream v0.0.0
replace example.com/upstream => ../shared
""",
    )
    _write(
        tmp_path,
        "app/main.go",
        'package main\nimport "example.com/upstream/client"\n',
    )
    _write(tmp_path, "shared/go.mod", "module example.com/shared\n\ngo 1.22\n")
    _write(tmp_path, "shared/client/client.go", "package client\n")

    assert ("app", "shared/client") in _links(tmp_path)

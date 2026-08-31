# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from tests.package_support import (
    InstalledTool,
    assert_tool_executable,
    environment,
    run,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def installed_package(tmp_path_factory) -> InstalledTool:
    uv = shutil.which("uv")
    assert uv is not None, "The packaging integration test requires uv"
    root = tmp_path_factory.mktemp("installed-package")
    source = root / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(
        ROOT / "blastradius",
        source / "blastradius",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    artifacts = root / "dist"
    run([uv, "build", "--sdist", str(source), "--out-dir", str(artifacts)], root)
    sdist = next(artifacts.glob("*.tar.gz"))
    run([uv, "build", "--wheel", str(sdist), "--out-dir", str(artifacts)], root)
    wheel = next(artifacts.glob("*.whl"))
    constraints = root / "constraints.txt"
    run(
        [
            uv,
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--output-file",
            str(constraints),
        ],
        ROOT,
    )
    tool_dir, bin_dir = root / "tools", root / "bin"
    env = {
        **environment(),
        "UV_TOOL_DIR": str(tool_dir),
        "UV_TOOL_BIN_DIR": str(bin_dir),
        "UV_CONSTRAINT": str(constraints),
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
    }
    run([uv, "tool", "install", "--python", sys.executable, str(wheel)], root, env=env)
    assert Path(run([uv, "tool", "dir", "--bin"], root, env=env).strip()) == bin_dir
    scripts = tool_dir / "blastradius-cli" / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    executable = bin_dir / ("blastradius.exe" if os.name == "nt" else "blastradius")
    assert_tool_executable(executable, env["PATH"])
    return InstalledTool(python, executable, env, sdist, wheel)

# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple


class InstalledTool(NamedTuple):
    python: Path
    executable: Path
    env: dict[str, str]
    sdist: Path
    wheel: Path


def assert_tool_executable(executable: Path, search_path: str) -> None:
    """Check that PATH selects the isolated installation under test."""
    discovered = shutil.which("blastradius", path=search_path)
    assert discovered is not None, "blastradius was not found on the tool PATH"
    assert executable.is_file(), f"Installed executable is missing: {executable}"
    assert Path(discovered).samefile(executable), (
        f"PATH selected {discovered}, not the isolated tool {executable}"
    )


def environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "UV_CONSTRAINT",
            "UV_OVERRIDE",
        }
        and not key.startswith("BLASTRADIUS_EMBEDDING_")
    }


def run(command, cwd, *, env=None, stdin=None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env if env is not None else environment(),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout

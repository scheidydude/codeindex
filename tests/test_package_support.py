# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

import shutil
from importlib.metadata import version
from pathlib import Path

import pytest

from blastradius import __version__
from tests.package_support import assert_tool_executable


def test_import_version_matches_distribution_metadata() -> None:
    assert __version__ == version("blastradius-cli")


def test_tool_discovery_accepts_different_case_for_the_same_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "blastradius.exe"
    executable.write_bytes(b"isolated tool")
    discovered = tmp_path / "BLASTRADIUS.EXE"
    # On case-sensitive hosts use a hard link to exercise the same identity
    # contract. Windows' case-insensitive filesystem already aliases the names.
    if not discovered.exists():
        discovered.hardlink_to(executable)
    assert discovered.samefile(executable)
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: str(discovered))

    assert_tool_executable(executable, str(tmp_path))


@pytest.mark.parametrize("found", [True, False])
def test_tool_discovery_rejects_wrong_or_missing_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, found: bool
) -> None:
    executable = tmp_path / "isolated" / "blastradius.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"isolated tool")
    other = tmp_path / "blastradius.exe"
    other.write_bytes(b"another installation")
    monkeypatch.setattr(
        shutil, "which", lambda *args, **kwargs: str(other) if found else None
    )

    with pytest.raises(AssertionError, match="not the isolated tool|not found"):
        assert_tool_executable(executable, str(tmp_path))

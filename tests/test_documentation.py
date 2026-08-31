# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installation_docs_use_uv_tools() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for command in (
        "uv tool install blastradius-cli",
        "uv tool update-shell",
        "uvx --from blastradius-cli blastradius --help",
        "uv tool upgrade blastradius-cli",
        "uv tool uninstall blastradius-cli",
        "uv tool install git+https://github.com/IWasZ3r0Cool/blast-radius.git",
    ):
        assert command in readme
    assert "pip install" not in readme
    assert "blastradius symbols . --watch" not in readme


def test_project_links_point_to_this_fork() -> None:
    documents = [*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")]
    project_links = []
    for document in documents:
        for owner, repository in re.findall(
            r"https://github\.com/([\w.-]+)/([\w.-]+)",
            document.read_text(encoding="utf-8"),
        ):
            repository = repository.removesuffix(".git")
            if repository.lower() in {"blastradius", "blast-radius", "blastradius-cli"}:
                project_links.append((owner, repository))
                assert (owner, repository) == ("IWasZ3r0Cool", "blast-radius"), document
    assert project_links, "Document the canonical repository URL"

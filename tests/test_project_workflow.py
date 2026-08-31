# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/add-to-project.yml"


def test_project_assignment_requires_explicit_project_credentials() -> None:
    workflow = WORKFLOW.read_text()

    assert "secrets.GITHUB_TOKEN" not in workflow
    assert "project-url: ${{ vars.ADD_TO_PROJECT_URL }}" in workflow
    assert "github-token: ${{ secrets.ADD_TO_PROJECT_PAT }}" in workflow
    assert "HAS_PROJECT_TOKEN: ${{ secrets.ADD_TO_PROJECT_PAT != '' }}" in workflow
    assert "PROJECT_URL: ${{ vars.ADD_TO_PROJECT_URL }}" in workflow
    assert "id: configuration" in workflow
    assert re.search(
        r"if: \$\{\{ steps\.configuration\.outputs\.configured == 'true' \}\}"
        r"\s+uses: actions/add-to-project@",
        workflow,
    )
    # Keep configured API errors visible instead of silently swallowing them.
    assert "continue-on-error" not in workflow


def test_project_action_uses_node24_release() -> None:
    workflow = WORKFLOW.read_text()

    assert (
        "actions/add-to-project@5afcf98fcd03f1c2f92c3c83f58ae24323cc57fd # v2.0.0"
        in workflow
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="Workflow runs in Bash")
@pytest.mark.parametrize(
    ("project_url", "has_token", "configured"),
    [
        ("", "false", False),
        ("", "true", False),
        ("https://github.com/users/example/projects/2", "false", False),
        ("https://github.com/users/example/projects/2", "true", True),
    ],
)
def test_project_configuration_gate(
    tmp_path: Path, project_url: str, has_token: str, configured: bool
) -> None:
    # Execute the actual workflow's inline preflight, without a YAML dependency
    # or a live GitHub token. The action's condition consumes this output.
    script = re.search(r"run: \|\n((?:[ ]{10}[^\n]*\n)+)", WORKFLOW.read_text())
    assert script is not None, "Project API calls need a configuration preflight"
    output = tmp_path / "github_output"

    result = subprocess.run(
        [
            shutil.which("bash"),
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            "-c",
            dedent(script.group(1)),
        ],
        env={
            **os.environ,
            "PROJECT_URL": project_url,
            "HAS_PROJECT_TOKEN": has_token,
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
        check=True,
    )

    assert output.read_text() == f"configured={str(configured).lower()}\n"
    if not configured:
        assert "Skipping project assignment" in result.stdout
        assert "ADD_TO_PROJECT_URL" in result.stdout
        assert "ADD_TO_PROJECT_PAT" in result.stdout

# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from importlib.metadata import version
from pathlib import Path

import pytest

from benchmark.mcp_client import MCPClient
from tests.package_support import environment

ROOT = Path(__file__).resolve().parents[1]


def launch(cwd, *args, env=None):
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("BLASTRADIUS_EMBEDDING_")
    }
    environment["PYTHONPATH"] = str(ROOT)
    return MCPClient(
        [sys.executable, "-m", "blastradius.cli", "serve", "--mcp", *map(str, args)],
        cwd,
        env={**environment, **(env or {})},
    )


@pytest.fixture
def client(tmp_path):
    with launch(tmp_path) as client:
        client.initialize()
        yield client


def test_malformed_message_does_not_disconnect_client(client):
    client.write_raw("null")
    response = client.receive()
    assert response["error"]["code"] == -32600
    assert len(client.send("tools/list")["result"]["tools"]) == 10


def payload(response):
    assert "error" not in response, response
    assert not response["result"].get("isError"), response
    return json.loads(response["result"]["content"][0]["text"])


@pytest.fixture
def repo(client, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "tests/fixtures/simple_python", repo)
    assert (
        payload(client.call_tool("analyze_repo", {"repo_path": str(repo)}))["files"]
        == 3
    )
    return repo


def export_graph(repo, graph):
    graph.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "blastradius.cli",
            "analyze",
            str(repo),
            "--output",
            str(graph),
        ],
        cwd=ROOT,
        env=environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=True,
    )


@pytest.fixture
def exported_graph(repo, tmp_path):
    graph = tmp_path / "exports" / "graph.json"
    export_graph(repo, graph)
    (repo / "blastradius.json").unlink()
    return graph


@pytest.mark.parametrize("tool", ["get_impact", "get_dependencies"])
def test_exported_graph_accepts_absolute_files_with_configured_repo(
    tmp_path, repo, exported_graph, tool
):
    with launch(tmp_path, "--repo", repo) as client:
        client.initialize()
        relative = payload(
            client.call_tool(
                tool, {"file_path": "models.py", "index_path": str(exported_graph)}
            )
        )
        absolute = payload(
            client.call_tool(
                tool,
                {
                    "file_path": str(repo / "models.py"),
                    "index_path": str(exported_graph),
                },
            )
        )
        assert absolute == relative
        assert absolute["file"] == "models.py"
        assert absolute["blast_score"] > 0


@pytest.mark.parametrize("tool", ["get_impact", "get_dependencies"])
@pytest.mark.parametrize("marker", ["graph", "database"])
def test_exported_graph_discovers_repository_above_startup_directory(
    tmp_path, repo, exported_graph, tool, marker
):
    if marker == "graph":
        shutil.copy2(exported_graph, repo / "blastradius.json")
        (repo / ".blastradius/index.db").unlink()
    startup = repo / "subdirectory" / "nested"
    startup.mkdir(parents=True)
    with launch(startup) as client:
        client.initialize()
        result = payload(
            client.call_tool(
                tool,
                {
                    "file_path": str(repo / "models.py"),
                    "index_path": str(exported_graph),
                },
            )
        )
        assert result["file"] == "models.py"
        assert result["blast_score"] > 0


@pytest.mark.parametrize("tool", ["get_impact", "get_dependencies"])
@pytest.mark.parametrize("configured_child", [False, True])
def test_exported_graph_requires_context_instead_of_guessing_absolute_paths(
    tmp_path, repo, exported_graph, tool, configured_child
):
    startup = repo / "child" if configured_child else tmp_path / "unrelated"
    startup.mkdir()
    args = ["--repo", startup] if configured_child else []
    with launch(startup, *args) as client:
        client.initialize()
        response = client.call_tool(
            tool,
            {
                "file_path": str(repo / "models.py"),
                "index_path": str(exported_graph),
            },
        )
        assert response["result"]["isError"] is True
        message = response["result"]["content"][0]["text"]
        assert "--repo" in message
        assert "repo-relative" in message
        assert (
            payload(
                client.call_tool(
                    tool, {"file_path": "models.py", "index_path": str(exported_graph)}
                )
            )["blast_score"]
            > 0
        )


@pytest.mark.parametrize("tool", ["get_impact", "get_dependencies"])
def test_exported_graph_rejects_conflicting_absolute_path_contexts(
    tmp_path, repo, tool
):
    nested = repo / "nested"
    nested.mkdir()
    (nested / "models.py").write_text("class Other: pass\n", encoding="utf-8")
    graph = nested / "graph.json"
    export_graph(repo, graph)
    with launch(tmp_path, "--repo", repo) as client:
        client.initialize()
        response = client.call_tool(
            tool,
            {"file_path": str(nested / "models.py"), "index_path": str(graph)},
        )
        assert response["result"]["isError"] is True
        message = response["result"]["content"][0]["text"]
        assert "Ambiguous" in message
        assert "models.py" in message and "nested/models.py" in message
        for file_path in ("models.py", "nested/models.py"):
            result = payload(
                client.call_tool(
                    tool, {"file_path": file_path, "index_path": str(graph)}
                )
            )
            assert result["file"] == file_path


@pytest.mark.parametrize("tool", ["get_impact", "get_dependencies"])
def test_explicit_graph_in_another_repository_remains_authoritative(
    tmp_path, repo, tool
):
    other = tmp_path / "other"
    other.mkdir()
    (other / "models.py").write_text("class Other: pass\n", encoding="utf-8")
    with launch(tmp_path, "--repo", repo) as client:
        client.initialize()
        payload(client.call_tool("analyze_repo", {"repo_path": str(other)}))
        selected = payload(
            client.call_tool(
                tool,
                {
                    "file_path": str(other / "models.py"),
                    "index_path": str(other / "blastradius.json"),
                },
            )
        )
        assert selected["file"] == "models.py"
        assert selected["blast_score"] == 0
        default = payload(
            client.call_tool(tool, {"file_path": str(repo / "models.py")})
        )
        assert default["blast_score"] > 0


def test_missing_indexed_file_is_a_tool_failure(client, repo):
    response = client.call_tool(
        "get_impact",
        {"file_path": "missing.py", "index_path": str(repo / "blastradius.json")},
    )
    assert response["result"].get("isError") is True
    assert "missing.py" in response["result"]["content"][0]["text"]
    assert (
        payload(
            client.call_tool(
                "get_dependencies",
                {"file_path": "utils.py", "index_path": str(repo / "blastradius.json")},
            )
        )["file"]
        == "utils.py"
    )


def test_invalid_tool_arguments_are_rejected_before_execution(client, repo):
    arguments = {
        "file": "utils.py",
        "db_path": str(repo / ".blastradius/index.db"),
        "direction": "sideways",
    }
    response = client.call_tool("graph_query", arguments)
    assert response["error"]["code"] == -32602
    assert "direction" in response["error"]["message"]
    arguments["direction"] = "dependents"
    result = payload(client.call_tool("graph_query", arguments))
    assert {node["file"] for node in result["nodes"]} == {"utils.py", "main.py"}


def test_current_impact_honors_database_outside_working_directory(client, repo):
    result = payload(
        client.call_tool(
            "temporal_impact",
            {"file": "models.py", "db_path": str(repo / ".blastradius/index.db")},
        )
    )
    assert result["file"] == "models.py"
    assert result["direct_dependents"] == 2


def test_configured_repository_is_used_for_relative_arguments(tmp_path, repo):
    with launch(tmp_path, "--repo", repo) as client:
        client.initialize()
        result = payload(
            client.call_tool("get_dependencies", {"file_path": "utils.py"})
        )
        assert result["imports"] == ["models.py"]
        result = payload(
            client.call_tool(
                "graph_query", {"file": "models.py", "db_path": ".blastradius/index.db"}
            )
        )
        assert {node["file"] for node in result["nodes"]} == {
            "models.py",
            "main.py",
            "utils.py",
        }


def test_explicit_symbol_index_wins_over_configured_database(tmp_path, repo):
    other = tmp_path / "other"
    shutil.copytree(ROOT / "tests/fixtures/simple_python", other)
    (other / "models.py").rename(other / "different.py")
    with launch(tmp_path, "--repo", repo) as client:
        client.initialize()
        payload(client.call_tool("build_symbol_index", {"repo_path": str(other)}))
        result = payload(
            client.call_tool(
                "lookup_symbol",
                {"name": "User", "symbol_index_path": str(other / "symbolindex.json")},
            )
        )
        assert result["matches"][0]["file"] == "different.py"
        assert (
            payload(client.call_tool("lookup_symbol", {"name": "User"}))["matches"][0][
                "file"
            ]
            == "models.py"
        )


def test_ambiguous_file_requires_an_explicit_path(tmp_path, client):
    repo = tmp_path / "ambiguous"
    for directory in ("a", "b"):
        target = repo / directory
        target.mkdir(parents=True)
        (target / "same.py").write_text("value = 1\n", encoding="utf-8")
    payload(client.call_tool("analyze_repo", {"repo_path": str(repo)}))
    response = client.call_tool(
        "get_impact",
        {"file_path": "same.py", "index_path": str(repo / "blastradius.json")},
    )
    assert response["result"].get("isError") is True
    assert "ambiguous" in response["result"]["content"][0]["text"].lower()
    assert (
        payload(
            client.call_tool(
                "get_dependencies",
                {
                    "file_path": "./a/same.py",
                    "index_path": str(repo / "blastradius.json"),
                },
            )
        )["file"]
        == "a/same.py"
    )


def test_search_invalid_ref_does_not_silently_search_current_data(client, repo):
    arguments = {
        "query": "User",
        "as_of": "missing-ref",
        "db_path": str(repo / ".blastradius/index.db"),
    }
    result = client.call_tool("semantic_search", arguments)["result"]
    assert result.get("isError") is True
    assert "missing-ref" in result["content"][0]["text"]
    arguments.pop("as_of")
    assert any(
        hit["name"] == "User"
        for hit in payload(client.call_tool("semantic_search", arguments))["results"]
    )


@pytest.mark.parametrize(
    "raw, code",
    [
        ("{", -32700),
        ("[]", -32600),
        ("17", -32600),
        ('"hello"', -32600),
        ('{"id":12,"method":"tools/list"}', -32600),
        ('{"jsonrpc":"1.0","id":12,"method":"tools/list"}', -32600),
        ('{"jsonrpc":"2.0","id":12,"method":7}', -32600),
        ('{"jsonrpc":"2.0","id":null,"method":"tools/list"}', -32600),
        ('{"jsonrpc":"2.0","id":true,"method":"tools/list"}', -32600),
        ('{"jsonrpc":"2.0","id":12,"method":"tools/call","params":null}', -32602),
        (
            '{"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":[]}}',
            -32602,
        ),
        (
            '{"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"graph_query","arguments":[]}}',
            -32602,
        ),
    ],
)
def test_protocol_errors_are_recoverable(client, raw, code):
    client.write_raw(raw)
    response = client.receive()
    assert response["error"]["code"] == code
    assert len(client.send("tools/list")["result"]["tools"]) == 10


def test_notifications_do_not_produce_replies(client):
    client.notify("notifications/initialized")
    client.notify("notifications/cancelled", {"requestId": "not-running"})
    client.notify("notifications/unknown", {"data": "ignored"})
    assert len(client.send("tools/list")["result"]["tools"]) == 10


@pytest.mark.parametrize(
    "name, arguments, field",
    [
        ("get_impact", {}, "file_path"),
        ("graph_query", {"file": 17}, "file"),
        ("graph_query", {"file": "models.py", "depth": -1}, "depth"),
        ("graph_query", {"file": "models.py", "depth": True}, "depth"),
        ("graph_query", {"file": "models.py", "depth": "2"}, "depth"),
        ("semantic_search", {"query": "User", "k": 0}, "k"),
        ("semantic_search", {"query": "User", "k": False}, "k"),
        ("get_high_blast_files", {"threshold": "5"}, "threshold"),
    ],
)
def test_input_schema_failures_are_actionable(client, name, arguments, field):
    error = client.call_tool(name, arguments)["error"]
    assert error["code"] == -32602
    assert field in error["message"]
    assert "Invalid" in error["message"]
    assert len(client.send("tools/list")["result"]["tools"]) == 10


def test_unknown_tool_is_a_protocol_error(client):
    response = client.call_tool("missing_tool", {})
    assert response["error"]["code"] == -32602
    assert "missing_tool" in response["error"]["message"]


def test_modern_discovery_and_every_advertised_revision(tmp_path):
    with launch(tmp_path) as client:
        client.version = "2026-07-28"
        result = client.send("server/discover")["result"]
        assert result["resultType"] == "complete"
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"][
            "version"
        ] == version("blastradius-cli")
        revisions = result["supportedVersions"]
        assert "2026-07-28" in revisions
    # Discovery lists modern revisions; legacy support uses initialization.
    for revision in set(revisions) | {
        "2024-11-05",
        "2025-03-26",
        "2025-06-18",
        "2025-11-25",
    }:
        with launch(tmp_path) as client:
            if revision >= "2026-07-28":
                client.version = revision
            else:
                assert client.initialize(revision)["serverInfo"]["version"] == version(
                    "blastradius-cli"
                )
            request_id = client.request("tools/list", request_id="catalog")
            response = client.receive()
            assert response["id"] == request_id
            assert len(response["result"]["tools"]) == 10
            if revision >= "2026-07-28":
                assert response["result"]["resultType"] == "complete"
            else:
                assert "resultType" not in response["result"]


def test_modern_unknown_version_is_rejected_and_recoverable(tmp_path):
    with launch(tmp_path) as client:
        client.version = "2099-01-01"
        response = client.send("tools/list")
        assert response["error"]["code"] == -32022
        assert "2026-07-28" in response["error"]["data"]["supported"]
        assert response["error"]["data"]["requested"] == "2099-01-01"
        client.version = "2026-07-28"
        assert len(client.send("tools/list")["result"]["tools"]) == 10


def test_eof_shutdown_is_clean(tmp_path):
    client = launch(tmp_path)
    client.initialize()
    assert client.close() == 0


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
def test_sdk_client_can_call_instantiated_server(tmp_path, repo, mode):
    import anyio
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    async def scenario():
        command = StdioServerParameters(
            command=sys.executable,
            args=["-m", "blastradius.cli", "serve", "--mcp", "--repo", str(repo)],
            cwd=tmp_path,
            env={"PYTHONPATH": str(ROOT)},
        )
        with anyio.fail_after(20):
            async with Client(command, mode=mode, read_timeout_seconds=10) as client:
                result = await client.call_tool(
                    "get_dependencies", {"file_path": "utils.py"}
                )
                assert not result.is_error
                assert json.loads(result.content[0].text)["imports"] == ["models.py"]

    anyio.run(scenario)


def git(repo, *args):
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("modern", [False, True])
def test_git_tools_finish_without_reading_protocol_input(tmp_path, modern):
    repo = tmp_path / "history"
    shutil.copytree(ROOT / "tests/fixtures/simple_python", repo)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Initial fixture")
    baseline = git(repo, "rev-parse", "HEAD")
    # Model a child inspecting inherited stdin (the Windows pipe-hang shape)
    # at the OS boundary. The real CLI, SDK, tools and Git commands still run.
    # No extra protocol message or EOF may be needed to unblock a tool call.
    script = """
import shutil
import subprocess
import sys
from blastradius.cli import main

git_executable = shutil.which("git")
class StdinSensitiveChild(subprocess.Popen):
    def __init__(self, command, *args, **kwargs):
        if command[0] == "git" and kwargs.get("stdin") is None:
            command = [
                sys.executable, "-c",
                "import os, sys; sys.stdin.buffer.read(1); os.execv(sys.argv[1], sys.argv[1:])",
                git_executable, *command[1:]
            ]
        super().__init__(command, *args, **kwargs)

subprocess.Popen = StdinSensitiveChild
main()
"""
    with MCPClient(
        [sys.executable, "-c", script, "serve", "--mcp", "--repo", str(repo)],
        tmp_path,
        env={**environment(), "PYTHONPATH": str(ROOT)},
    ) as client:
        if modern:
            client.version = "2026-07-28"
            assert len(client.send("tools/list")["result"]["tools"]) == 10
        else:
            client.initialize()
        client.timeout = 5
        assert (
            payload(client.call_tool("analyze_repo", {"repo_path": "."}))["files"] == 3
        )
        (repo / "main.py").write_text(
            (repo / "main.py").read_text(encoding="utf-8") + "\nnew_value = 42\n",
            encoding="utf-8",
        )
        git(repo, "add", "main.py")
        git(repo, "commit", "-m", "Modify main")
        payload(client.call_tool("analyze_repo", {"repo_path": "."}))
        assert payload(client.call_tool("changed_since", {"ref": baseline}))[
            "modified_files"
        ] == ["main.py"]
        assert (
            payload(
                client.call_tool(
                    "temporal_impact", {"file": "models.py", "as_of": baseline}
                )
            )["direct_dependents"]
            == 2
        )
        assert len(client.send("tools/list")["result"]["tools"]) == 10


def test_history_backfill_does_not_inherit_server_input(tmp_path, monkeypatch):
    from blastradius.index import build
    from blastradius.store import Store
    from blastradius.temporal import backfill

    repo = tmp_path / "history"
    shutil.copytree(ROOT / "tests/fixtures/simple_python", repo)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Initial fixture")
    build(str(repo))
    popen = subprocess.Popen

    def isolated_child(command, *args, **kwargs):
        if command[0] == "git" and kwargs.get("stdin") is None:
            raise AssertionError("Git child would inherit the server input pipe")
        return popen(command, *args, **kwargs)

    # Guard the process boundary; exercise real Git log/tree/blob reading and
    # storage, including cat-file's separate input pipe for its batch queries.
    monkeypatch.setattr(subprocess, "Popen", isolated_child)
    with closing(Store(repo / ".blastradius/index.db")) as store:
        assert backfill(repo, store) == (1, 3)


def test_all_ten_tools_return_meaningful_results_with_history(tmp_path):
    repo = tmp_path / "history"
    shutil.copytree(ROOT / "tests/fixtures/simple_python", repo)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Initial fixture")
    baseline = git(repo, "rev-parse", "HEAD")
    with launch(tmp_path, "--repo", repo) as client:
        client.version = "2026-07-28"
        assert (
            payload(client.call_tool("analyze_repo", {"repo_path": "."}))["files"] == 3
        )
        assert (
            payload(client.call_tool("build_symbol_index", {"repo_path": "."}))[
                "total_symbols"
            ]
            > 0
        )
        assert (
            payload(client.call_tool("lookup_symbol", {"name": "User"}))["matches"][0][
                "file"
            ]
            == "models.py"
        )
        assert payload(
            client.call_tool("lookup_symbol", {"name": "MissingSymbol"})
        ) == {"found": False, "name": "MissingSymbol", "matches": []}
        assert (
            payload(client.call_tool("get_impact", {"file_path": "models.py"}))[
                "blast_score"
            ]
            > 0
        )
        assert payload(client.call_tool("get_dependencies", {"file_path": "utils.py"}))[
            "imports"
        ] == ["models.py"]
        high = payload(client.call_tool("get_high_blast_files", {"threshold": 0}))
        assert high["count"] == 3
        assert high["files"][0]["file"] == "models.py"
        graph = payload(
            client.call_tool(
                "graph_query",
                {"file": "models.py", "direction": "dependents", "depth": 0},
            )
        )
        assert [n["file"] for n in graph["nodes"]] == ["models.py"]
        (repo / "main.py").write_text(
            (repo / "main.py").read_text() + "\nnew_value = 42\n", encoding="utf-8"
        )
        git(repo, "add", "main.py")
        git(repo, "commit", "-m", "Modify main")
        payload(client.call_tool("analyze_repo", {"repo_path": "."}))
        assert payload(client.call_tool("changed_since", {"ref": baseline}))[
            "modified_files"
        ] == ["main.py"]
        temporal = payload(
            client.call_tool(
                "temporal_impact", {"file": "models.py", "as_of": baseline}
            )
        )
        assert temporal["direct_dependents"] == 2
        search = payload(
            client.call_tool("semantic_search", {"query": "User", "as_of": baseline})
        )
        assert any(r["name"] == "User" for r in search["results"])


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="Deterministic blocked file read requires POSIX FIFO",
)
@pytest.mark.parametrize("modern", [False, True])
def test_cancel_running_and_queued_tools_without_late_replies(tmp_path, modern):
    slow = tmp_path / "slow"
    slow.mkdir()
    source = slow / "slow.py"
    os.mkfifo(source)
    queued = tmp_path / "queued"
    queued.mkdir()
    (queued / "never.py").write_text("class Never: pass\n", encoding="utf-8")
    with launch(tmp_path) as client:
        if modern:
            client.version = "2026-07-28"
        else:
            client.initialize()
        running = client.request(
            "tools/call",
            {"name": "build_symbol_index", "arguments": {"repo_path": str(slow)}},
        )
        deadline = time.monotonic() + 10
        writer = None
        try:
            while writer is None and time.monotonic() < deadline:
                try:
                    writer = os.open(source, os.O_WRONLY | os.O_NONBLOCK)
                except OSError as exc:
                    if exc.errno != errno.ENXIO:
                        raise
                    time.sleep(0.01)
            assert writer is not None, "Tool did not start reading the fixture"
            queued_id = client.request(
                "tools/call",
                {"name": "build_symbol_index", "arguments": {"repo_path": str(queued)}},
            )
            client.notify("notifications/cancelled", {"requestId": queued_id})
            client.notify("notifications/cancelled", {"requestId": running})
            # A catalog reply is a protocol barrier, not a guessed cancellation delay.
            assert len(client.send("tools/list")["result"]["tools"]) == 10
            os.write(writer, b"class Completed: pass\n")
        finally:
            if writer is not None:
                os.close(writer)
        result = payload(
            client.call_tool(
                "lookup_symbol",
                {
                    "name": "Completed",
                    "symbol_index_path": str(slow / "symbolindex.json"),
                },
            )
        )
        assert result["found"] is True  # Running writes finish safely.
        result = client.call_tool(
            "lookup_symbol",
            {"name": "Never", "symbol_index_path": str(queued / "symbolindex.json")},
        )
        assert result["result"]["isError"] is True  # Queued work never wrote an index.


def test_unicode_paths_work_with_non_utf8_parent_settings(tmp_path):
    repo = tmp_path / "répertoire_日本"
    repo.mkdir()
    (repo / "café.py").write_text("class Café: pass\n", encoding="utf-8")
    with launch(
        tmp_path,
        "--repo",
        repo,
        env={"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
    ) as client:
        client.initialize()
        payload(client.call_tool("analyze_repo", {"repo_path": str(repo)}))
        result = payload(client.call_tool("lookup_symbol", {"name": "Café"}))
        assert result["matches"][0]["file"] == "café.py"


def test_explicit_artifacts_never_fall_back_to_another_repository(tmp_path, repo):
    missing = tmp_path / "missing"
    missing.mkdir()
    with launch(repo, "--repo", missing) as client:
        client.initialize()
        assert (
            client.call_tool("get_dependencies", {"file_path": "models.py"})["result"][
                "isError"
            ]
            is True
        )
        assert (
            payload(
                client.call_tool(
                    "get_dependencies",
                    {
                        "file_path": str(repo / "models.py"),
                        "index_path": str(repo / "blastradius.json"),
                    },
                )
            )["file"]
            == "models.py"
        )
        assert (
            client.call_tool(
                "lookup_symbol", {"name": "User", "symbol_index_path": "missing.json"}
            )["result"]["isError"]
            is True
        )
        (missing / "broken.json").write_text("{invalid", encoding="utf-8")
        assert (
            client.call_tool(
                "get_dependencies",
                {"file_path": "models.py", "index_path": "broken.json"},
            )["result"]["isError"]
            is True
        )


@pytest.mark.parametrize("launcher", ["tool", "uvx"])
@pytest.mark.parametrize("modern", [False, True])
def test_installed_launchers_complete_tool_round_trip(
    installed_package, tmp_path, launcher, modern
):
    repo = tmp_path / "repository"
    shutil.copytree(ROOT / "tests/fixtures/simple_python", repo)
    if launcher == "tool":
        command = [str(installed_package.executable)]
    else:
        command = [
            shutil.which("uvx"),
            "--isolated",
            "--python",
            str(installed_package.python),
            "--from",
            str(installed_package.wheel),
            "blastradius",
        ]
    with MCPClient(
        [*command, "serve", "--mcp", "--repo", str(repo)],
        tmp_path,
        env=installed_package.env,
    ) as client:
        if modern:
            client.version = "2026-07-28"
            assert "tools" in client.send("server/discover")["result"]["capabilities"]
        else:
            client.initialize()
        assert (
            payload(client.call_tool("analyze_repo", {"repo_path": "."}))["files"] == 3
        )
        assert payload(client.call_tool("get_dependencies", {"file_path": "utils.py"}))[
            "imports"
        ] == ["models.py"]
        assert (
            client.call_tool("graph_query", {"file": "models.py", "depth": -1})[
                "error"
            ]["code"]
            == -32602
        )


def test_analyzing_another_repo_does_not_change_default(tmp_path, repo):
    other = tmp_path / "other"
    other.mkdir()
    (other / "models.py").write_text("class Other: pass\n", encoding="utf-8")
    with launch(tmp_path, "--repo", repo) as client:
        client.initialize()
        payload(client.call_tool("analyze_repo", {"repo_path": str(other)}))
        assert (
            payload(client.call_tool("lookup_symbol", {"name": "User"}))["matches"][0][
                "file"
            ]
            == "models.py"
        )
        assert (
            payload(
                client.call_tool(
                    "get_dependencies",
                    {
                        "file_path": "models.py",
                        "index_path": str(other / "blastradius.json"),
                    },
                )
            )["imported_by"]
            == []
        )
        (other / "blastradius.json").unlink()
        assert (
            client.call_tool(
                "temporal_impact",
                {"file": "models.py", "db_path": str(other / ".blastradius/index.db")},
            )["result"]["isError"]
            is True
        )


def test_eof_before_initialization_is_clean(tmp_path):
    client = launch(tmp_path)
    assert client.close() == 0


def test_nonfinite_json_number_is_a_parse_error(client):
    client.write_raw(
        '{"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"get_high_blast_files","arguments":{"threshold":NaN}}}'
    )
    assert client.receive()["error"]["code"] == -32700
    assert len(client.send("tools/list")["result"]["tools"]) == 10


def test_null_arguments_is_not_an_empty_argument_object(client):
    client.write_raw(
        '{"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"get_high_blast_files","arguments":null}}'
    )
    assert client.receive()["error"]["code"] == -32602


def test_analysis_of_nonexistent_directory_is_a_tool_failure(client, tmp_path):
    response = client.call_tool("analyze_repo", {"repo_path": str(tmp_path / "absent")})
    assert response["result"]["isError"] is True
    assert "absent" in response["result"]["content"][0]["text"]


def test_corrupt_database_is_actionable_without_leaking_connections(tmp_path):
    database = tmp_path / "broken.db"
    database.write_bytes(b"not a SQLite database")
    with launch(tmp_path, env={"PYTHONWARNINGS": "always::ResourceWarning"}) as client:
        client.initialize()
        for _ in range(3):
            response = client.call_tool(
                "semantic_search", {"query": "User", "db_path": str(database)}
            )
            assert response["result"]["isError"] is True
            assert "database" in response["result"]["content"][0]["text"]
        assert len(client.send("tools/list")["result"]["tools"]) == 10
    assert "ResourceWarning" not in "".join(client.stderr)


def test_failed_index_write_releases_database_and_server_recovers(tmp_path, repo):
    database = sqlite3.connect(repo / ".blastradius/index.db")
    try:
        database.execute(
            "CREATE TRIGGER fail_write BEFORE UPDATE ON files "
            "BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END"
        )
        database.commit()
    finally:
        database.close()
    with launch(
        tmp_path, "--repo", repo, env={"PYTHONWARNINGS": "always::ResourceWarning"}
    ) as client:
        client.initialize()
        response = client.call_tool("analyze_repo", {"repo_path": "."})
        assert response["result"]["isError"] is True
        assert "fixture write failure" in response["result"]["content"][0]["text"]
        assert payload(client.call_tool("graph_query", {"file": "models.py"}))["nodes"]
    assert "ResourceWarning" not in "".join(client.stderr)

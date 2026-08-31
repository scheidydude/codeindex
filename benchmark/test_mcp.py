#!/usr/bin/env python3
# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

"""
MCP server integration test — exercises all tools via real JSON-RPC stdio.

Usage:
  python benchmark/test_mcp.py [--repo PATH] [--blastradius PATH]

Starts the MCP server as a subprocess, sends JSON-RPC messages, validates responses.
Checks discovery of all ten tools and exercises the six original tools.
The pytest MCP suite covers all ten tools, including isolated Git history.
"""

import json
import sys
from pathlib import Path

if __package__:
    from benchmark.mcp_client import MCPClient
else:
    from mcp_client import MCPClient


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------
PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  {PASS} {label}")
        else:
            self.failed += 1
            print(f"  {FAIL} {label}" + (f"\n      {detail}" if detail else ""))

    def summary(self) -> None:
        total = self.passed + self.failed
        print(f"\n{'─' * 50}")
        print(f"  {self.passed}/{total} passed", end="")
        if self.failed:
            print(f"  ({self.failed} failed)")
        else:
            print("  — all good")
        print(f"{'─' * 50}")


def _result_text(response: dict) -> str:
    try:
        return response["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return ""


def _result_data(response: dict) -> dict:
    try:
        return json.loads(_result_text(response))
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return {}


def _is_error(response: dict) -> bool:
    if "error" in response:
        return True
    try:
        return response["result"].get("isError", False)
    except (KeyError, AttributeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_initialize(client: MCPClient, r: Results) -> None:
    print("\n── initialize ──")
    resp = client.send(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.0.1"},
        },
    )
    r.check("no error", "error" not in resp, str(resp.get("error")))
    result = resp.get("result", {})
    r.check("protocolVersion present", "protocolVersion" in result)
    r.check("tools capability present", "tools" in result.get("capabilities", {}))
    r.check("serverInfo present", "serverInfo" in result)
    client.notify("notifications/initialized")


def test_tools_list(client: MCPClient, r: Results) -> None:
    print("\n── tools/list ──")
    resp = client.send("tools/list")
    r.check("no error", "error" not in resp, str(resp.get("error")))
    tools = {t["name"] for t in resp.get("result", {}).get("tools", [])}
    for name in [
        "analyze_repo",
        "get_impact",
        "get_dependencies",
        "get_high_blast_files",
        "build_symbol_index",
        "lookup_symbol",
        "semantic_search",
        "temporal_impact",
        "graph_query",
        "changed_since",
    ]:
        r.check(f"tool registered: {name}", name in tools)


def test_analyze_repo(client: MCPClient, r: Results, repo: str) -> None:
    print("\n── analyze_repo ──")
    resp = client.call_tool("analyze_repo", {"repo_path": repo})
    r.check("no error", not _is_error(resp), _result_text(resp))
    data = _result_data(resp)
    r.check("success=true", data.get("success") is True)
    r.check("files count > 0", data.get("files", 0) > 0, str(data))
    r.check("languages present", isinstance(data.get("languages"), list))


def test_get_impact(client: MCPClient, r: Results, file: str) -> None:
    print("\n── get_impact ──")
    resp = client.call_tool("get_impact", {"file_path": file})
    r.check("no error", not _is_error(resp), _result_text(resp))
    data = _result_data(resp)
    r.check(f"file field present ({file})", "file" in data, str(data))
    r.check("blast_score present", "blast_score" in data)
    r.check("report present", "report" in data)

    # Unknown files are execution errors, not successful JSON payloads.
    resp2 = client.call_tool("get_impact", {"file_path": "nonexistent/file.py"})
    r.check(
        "unknown file returns an actionable tool error",
        resp2.get("result", {}).get("isError") is True
        and "nonexistent/file.py" in _result_text(resp2),
        str(resp2),
    )


def test_get_dependencies(client: MCPClient, r: Results, file: str) -> None:
    print("\n── get_dependencies ──")
    resp = client.call_tool("get_dependencies", {"file_path": file})
    r.check("no error", not _is_error(resp), _result_text(resp))
    data = _result_data(resp)
    r.check(f"file field present ({file})", "file" in data, str(data))
    r.check("imports list present", isinstance(data.get("imports"), list))
    r.check("imported_by list present", isinstance(data.get("imported_by"), list))
    r.check("blast_score present", "blast_score" in data)


def test_get_high_blast_files(client: MCPClient, r: Results) -> None:
    print("\n── get_high_blast_files ──")
    resp = client.call_tool("get_high_blast_files", {"threshold": 1})
    r.check("no error", not _is_error(resp), _result_text(resp))
    data = _result_data(resp)
    r.check("files list present", isinstance(data.get("files"), list))
    r.check(
        "count matches list length", data.get("count") == len(data.get("files", []))
    )
    if data.get("files"):
        first = data["files"][0]
        r.check("file has blast_score", "blast_score" in first)
        r.check(
            "results sorted descending",
            all(
                data["files"][i]["blast_score"] >= data["files"][i + 1]["blast_score"]
                for i in range(len(data["files"]) - 1)
            ),
        )

    # High threshold should return empty list, not crash
    resp2 = client.call_tool("get_high_blast_files", {"threshold": 9999})
    data2 = _result_data(resp2)
    r.check("high threshold returns empty list", data2.get("count", -1) == 0)


def test_build_symbol_index(client: MCPClient, r: Results, repo: str) -> None:
    print("\n── build_symbol_index ──")
    resp = client.call_tool("build_symbol_index", {"repo_path": repo})
    r.check("no error", not _is_error(resp), _result_text(resp))
    data = _result_data(resp)
    r.check("success=true", data.get("success") is True)
    r.check("total_symbols > 0", data.get("total_symbols", 0) > 0, str(data))
    r.check("files > 0", data.get("files", 0) > 0)
    r.check("output path present", "output" in data)


def test_lookup_symbol(
    client: MCPClient, r: Results, symbols: list[tuple[str, str, int]]
) -> None:
    print("\n── lookup_symbol ──")

    for sym_name, expected_file, expected_line in symbols[:2]:
        resp = client.call_tool("lookup_symbol", {"name": sym_name})
        r.check(f"no error ({sym_name})", not _is_error(resp), _result_text(resp))
        data = _result_data(resp)
        r.check(f"found=true ({sym_name})", data.get("found") is True, str(data))
        r.check("matches list present", isinstance(data.get("matches"), list))
        if data.get("matches"):
            m = data["matches"][0]
            r.check("match has file", "file" in m)
            r.check("match has line", "line" in m)
            r.check("match has kind", "kind" in m)
            r.check(
                f"correct file ({expected_file})",
                expected_file in m.get("file", ""),
                m.get("file"),
            )
            r.check(
                f"correct line ({expected_line})",
                m.get("line") == expected_line,
                f"got line {m.get('line')}",
            )

    # Unknown symbol
    resp3 = client.call_tool("lookup_symbol", {"name": "__nonexistent_symbol_xyz__"})
    data3 = _result_data(resp3)
    r.check(
        "unknown symbol returns found=false", data3.get("found") is False, str(data3)
    )


def test_unknown_tool(client: MCPClient, r: Results) -> None:
    print("\n── error handling ──")
    resp = client.call_tool("nonexistent_tool", {})
    r.check("unknown tool returns error", "error" in resp or _is_error(resp), str(resp))

    resp2 = client.send("nonexistent/method")
    r.check("unknown method returns error", "error" in resp2, str(resp2))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    import argparse

    parser = argparse.ArgumentParser(description="MCP server integration tests")
    parser.add_argument("--repo", default=".", help="Repo path (default: .)")
    parser.add_argument(
        "--blastradius",
        default="blastradius",
        help="Path to blastradius executable (default: blastradius)",
    )
    args = parser.parse_args()

    repo = str(Path(args.repo).resolve())
    command = [args.blastradius, "serve", "--mcp"]

    print(f"Repo     : {repo}")
    print(f"Command  : {' '.join(command)}")
    print("Starting MCP server…")

    # Discover test fixtures from the repo's own indexes
    index_file = Path(repo) / "blastradius.json"
    sym_file = Path(repo) / "symbolindex.json"

    probe_file = "."
    if index_file.exists():
        index_data = json.loads(index_file.read_text())
        nodes = [
            n["id"] for n in index_data.get("nodes", []) if n.get("type") != "import"
        ]
        if nodes:
            # Pick the node with the highest blast score as a good probe target
            scored = sorted(
                index_data.get("nodes", []),
                key=lambda n: n.get("blast_score", 0),
                reverse=True,
            )
            probe_file = next(
                (n["id"] for n in scored if n.get("type") != "import"), nodes[0]
            )
            print(f"Probe file: {probe_file}")

    probe_symbols: list[tuple[str, str, int]] = []
    if sym_file.exists():
        sym_data = json.loads(sym_file.read_text())
        for name, matches in list(sym_data.get("symbols", {}).items())[:2]:
            m = matches[0]
            probe_symbols.append((name, m["file"].split("/")[-1], m["line"]))
        print(f"Probe symbols: {[s[0] for s in probe_symbols]}")

    r = Results()
    client = MCPClient(command, cwd=repo)
    try:
        test_initialize(client, r)
        test_tools_list(client, r)
        test_analyze_repo(client, r, repo)
        test_get_impact(client, r, probe_file)
        test_get_dependencies(client, r, probe_file)
        test_get_high_blast_files(client, r)
        test_build_symbol_index(client, r, repo)
        test_lookup_symbol(client, r, probe_symbols)
        test_unknown_tool(client, r)
    finally:
        client.close()

    r.summary()
    sys.exit(0 if r.failed == 0 else 1)


if __name__ == "__main__":
    main()

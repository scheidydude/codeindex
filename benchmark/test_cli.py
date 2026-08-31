#!/usr/bin/env python3
"""
CLI integration tests for: lookup, dependencies, high-blast.

Usage:
  python benchmark/test_cli.py [--repo PATH] [--blastradius PATH]

Reads blastradius.json and symbolindex.json from --repo to build fixtures,
then invokes the CLI as a subprocess and validates stdout/exit codes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
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


def run(cmd: list[str], cwd: str) -> tuple[int, str, str]:
    """Run command, return (returncode, stdout, stderr)."""
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, check=False)
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# lookup tests
# ---------------------------------------------------------------------------
def test_lookup(
    exe: str, repo: str, r: Results, sym_name: str, expected_file: str
) -> None:
    print("\n── lookup ──")

    # happy path: human-readable output
    rc, out, err = run(
        [exe, "lookup", sym_name, "--index", str(Path(repo) / "symbolindex.json")], repo
    )
    r.check("exit 0 for known symbol", rc == 0, err.strip())
    r.check("output contains file", expected_file in out, repr(out[:200]))
    r.check(
        "output contains line number", any(c.isdigit() for c in out), repr(out[:200])
    )

    # --json flag
    rc, out, err = run(
        [
            exe,
            "lookup",
            sym_name,
            "--json",
            "--index",
            str(Path(repo) / "symbolindex.json"),
        ],
        repo,
    )
    r.check("--json exit 0", rc == 0, err.strip())
    try:
        data = json.loads(out)
        r.check("--json name field", data.get("name") == sym_name)
        r.check(
            "--json matches list",
            isinstance(data.get("matches"), list) and len(data["matches"]) > 0,
        )
        m = data["matches"][0]
        r.check("--json match has file", "file" in m)
        r.check("--json match has line", "line" in m)
        r.check("--json match has kind", "kind" in m)
    except json.JSONDecodeError as exc:
        r.check("--json parses", False, str(exc))

    # unknown symbol → exit non-zero + stderr message
    rc, out, err = run(
        [
            exe,
            "lookup",
            "__nonexistent_xyz__",
            "--index",
            str(Path(repo) / "symbolindex.json"),
        ],
        repo,
    )
    r.check("exit non-zero for unknown symbol", rc != 0)
    r.check(
        "stderr message for unknown symbol",
        "not found" in err.lower() or "not found" in out.lower(),
        repr(err),
    )


# ---------------------------------------------------------------------------
# dependencies tests
# ---------------------------------------------------------------------------
def test_dependencies(
    exe: str, repo: str, r: Results, probe_file: str, index_path: str
) -> None:
    print("\n── dependencies ──")

    # happy path: human-readable output
    rc, out, err = run([exe, "dependencies", probe_file, "--index", index_path], repo)
    r.check("exit 0 for known file", rc == 0, err.strip())
    r.check("output contains 'File:'", "File:" in out, repr(out[:200]))
    r.check("output contains 'Imports'", "Imports" in out, repr(out[:200]))
    r.check("output contains 'Imported by'", "Imported by" in out, repr(out[:200]))

    # --json flag
    rc, out, err = run(
        [exe, "dependencies", probe_file, "--json", "--index", index_path], repo
    )
    r.check("--json exit 0", rc == 0, err.strip())
    try:
        data = json.loads(out)
        r.check("--json file field", "file" in data, repr(out[:200]))
        r.check("--json imports list", isinstance(data.get("imports"), list))
        r.check("--json imported_by list", isinstance(data.get("imported_by"), list))
        r.check("--json blast_score present", "blast_score" in data)
    except json.JSONDecodeError as exc:
        r.check("--json parses", False, str(exc))

    # unknown file → exit non-zero + stderr message
    rc, out, err = run(
        [exe, "dependencies", "nonexistent/ghost.py", "--index", index_path], repo
    )
    r.check("exit non-zero for unknown file", rc != 0)
    r.check("stderr message for unknown file", "not found" in err.lower(), repr(err))

    # missing index → exit non-zero + helpful message
    rc, out, err = run(
        [exe, "dependencies", probe_file, "--index", "/tmp/no_such_index.json"], repo
    )
    r.check("exit non-zero for missing index", rc != 0)


# ---------------------------------------------------------------------------
# high-blast tests
# ---------------------------------------------------------------------------
def test_high_blast(exe: str, repo: str, r: Results, index_path: str) -> None:
    print("\n── high-blast ──")

    # default threshold: human-readable
    rc, out, err = run([exe, "high-blast", "--index", index_path], repo)
    r.check("exit 0", rc == 0, err.strip())
    r.check(
        "output contains 'blast score'", "blast score" in out.lower(), repr(out[:200])
    )

    # --json with low threshold (should return results)
    rc, out, err = run(
        [exe, "high-blast", "--threshold", "0", "--json", "--index", index_path], repo
    )
    r.check("--json exit 0", rc == 0, err.strip())
    try:
        data = json.loads(out)
        r.check("--json threshold field", "threshold" in data)
        r.check("--json count field", "count" in data)
        r.check("--json files list", isinstance(data.get("files"), list))
        r.check(
            "--json count matches list length",
            data.get("count") == len(data.get("files", [])),
        )
        if data["files"]:
            first = data["files"][0]
            r.check("each entry has file", "file" in first)
            r.check("each entry has blast_score", "blast_score" in first)
            r.check(
                "results sorted descending",
                all(
                    data["files"][i]["blast_score"]
                    >= data["files"][i + 1]["blast_score"]
                    for i in range(len(data["files"]) - 1)
                ),
                "not sorted",
            )
    except json.JSONDecodeError as exc:
        r.check("--json parses", False, str(exc))

    # very high threshold → empty result, exit 0
    rc, out, err = run(
        [exe, "high-blast", "--threshold", "99999", "--json", "--index", index_path],
        repo,
    )
    r.check("exit 0 for empty result", rc == 0, err.strip())
    try:
        data = json.loads(out)
        r.check("empty result count=0", data.get("count") == 0)
        r.check("empty result files=[]", data.get("files") == [])
    except json.JSONDecodeError as exc:
        r.check("--json parses for empty result", False, str(exc))

    # missing index → exit non-zero
    rc, out, err = run([exe, "high-blast", "--index", "/tmp/no_such_index.json"], repo)
    r.check("exit non-zero for missing index", rc != 0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    import argparse

    parser = argparse.ArgumentParser(
        description="CLI integration tests: lookup, dependencies, high-blast"
    )
    parser.add_argument("--repo", default=".", help="Repo path (default: .)")
    parser.add_argument(
        "--blastradius",
        default="blastradius",
        help="Path to blastradius executable (default: blastradius)",
    )
    args = parser.parse_args()

    repo = str(Path(args.repo).resolve())
    exe = args.blastradius

    print(f"Repo     : {repo}")
    print(f"Command  : {exe}")

    index_path = str(Path(repo) / "blastradius.json")
    sym_path = str(Path(repo) / "symbolindex.json")

    if not Path(index_path).exists():
        print(f"Building index: {exe} analyze {repo}")
        rc, _out, err = run([exe, "analyze", repo], repo)
        if rc != 0 or not Path(index_path).exists():
            print(f"ERROR: Failed to build {index_path}.\n{err}", file=sys.stderr)
            sys.exit(1)

    if not Path(sym_path).exists():
        print(f"Building symbol index: {exe} symbols {repo}")
        rc, _out, err = run([exe, "symbols", repo], repo)
        if rc != 0 or not Path(sym_path).exists():
            print(f"ERROR: Failed to build {sym_path}.\n{err}", file=sys.stderr)
            sys.exit(1)

    # Pick probe fixtures from actual index data
    index_data = json.loads(Path(index_path).read_text())
    sym_data = json.loads(Path(sym_path).read_text())

    # Best probe file: highest blast score, non-import node
    scored = sorted(
        [n for n in index_data["nodes"] if n.get("type") != "import"],
        key=lambda n: n.get("blast_score", 0),
        reverse=True,
    )
    probe_file = scored[0]["id"] if scored else index_data["nodes"][0]["id"]

    # Probe symbol: first symbol in index
    symbols = sym_data.get("symbols", {})
    if not symbols:
        print(
            "ERROR: symbolindex.json has no symbols. Run: blastradius symbols {repo}",
            file=sys.stderr,
        )
        sys.exit(1)
    sym_name, sym_matches = next(iter(symbols.items()))
    expected_file = Path(sym_matches[0]["file"]).name

    print(f"Probe file  : {probe_file}")
    print(f"Probe symbol: {sym_name}  (expected in file containing '{expected_file}')")

    r = Results()
    test_lookup(exe, repo, r, sym_name, expected_file)
    test_dependencies(exe, repo, r, probe_file, index_path)
    test_high_blast(exe, repo, r, index_path)

    r.summary()
    sys.exit(0 if r.failed == 0 else 1)


if __name__ == "__main__":
    main()

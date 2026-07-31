"""Shared utilities for all language analyzers."""
import re
from pathlib import Path

# Directories to skip during file collection
SKIP_DIRS = {
    "__pycache__", "node_modules", ".venv", "venv", "env",
    ".git", "dist", "build", ".next", "out", "coverage",
    ".turbo", ".cache", "target", "vendor", ".expo",
}


def load_gitignore_patterns(root: Path) -> list:
    gi = root / ".gitignore"
    patterns = []
    if gi.exists():
        for line in gi.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pat = (
                re.escape(line)
                .replace(r"\*\*", ".*")
                .replace(r"\*", "[^/]*")
                .replace(r"\?", ".")
            )
            patterns.append(re.compile(pat))
    return patterns


def is_ignored(path: Path, root: Path, patterns: list) -> bool:
    rel = str(path.relative_to(root))
    return any(pat.search(rel) for pat in patterns)


def is_skip_dir(path: Path) -> bool:
    return any(part.startswith(".") or part in SKIP_DIRS for part in path.parts)


def dir_group(path: Path, root: Path, group_map: dict) -> int:
    """Assign a numeric group id based on the file's parent directory."""
    rel = path.relative_to(root)
    key = str(rel.parent) if str(rel.parent) != "." else ""
    if key not in group_map:
        group_map[key] = len(group_map)
    return group_map[key]

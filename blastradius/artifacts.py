# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
from __future__ import annotations

"""Resolve one request's repository artifacts without changing process state."""

import json
import posixpath
from contextlib import ExitStack
from pathlib import Path


class Artifacts(ExitStack):
    """Per-call file selection and database lifetime, owned by the calling thread."""

    def __init__(self, base: Path, *, discover: bool = True):
        super().__init__()
        self.base = base.resolve()
        self.discover = discover

    def path(self, value: str | Path) -> Path:
        return (self.base / value).resolve()

    def find(self, name: str, explicit: str | Path | None = None) -> Path:
        if explicit is not None:
            candidate = self.path(explicit)
            if candidate.is_file():
                return candidate
        else:
            roots = [self.base, *self.base.parents] if self.discover else [self.base]
            for root in roots:
                candidate = root / name
                if candidate.is_file():
                    return candidate
        command = "symbols" if name == "symbolindex.json" else "analyze"
        target = self.path(explicit) if explicit is not None else self.base / name
        raise FileNotFoundError(
            f"{target} not found. Run: blastradius {command} {self.base}"
        )

    def index(self, explicit: str | Path | None = None) -> dict:
        return json.loads(
            self.find("blastradius.json", explicit).read_text(encoding="utf-8")
        )

    def index_for_file(
        self, file_path: str, explicit: str | Path | None = None
    ) -> tuple[dict, str]:
        """Select one graph, then resolve a file without substituting graph data."""
        graph_path = self.find("blastradius.json", explicit)
        data = json.loads(graph_path.read_text(encoding="utf-8"))
        node_ids = {node["id"] for node in data["nodes"]}
        path = Path(file_path)
        if path.is_absolute():
            matches = set()
            for root in {graph_path.parent, self._file_context_root()}:
                try:
                    candidate = path.resolve().relative_to(root).as_posix()
                except ValueError:
                    continue
                if candidate in node_ids:
                    matches.add(candidate)
            if len(matches) > 1:
                raise ValueError(
                    f"Ambiguous file {file_path}: {', '.join(sorted(matches))}; "
                    "use a repo-relative path from the selected graph"
                )
            file_id = next(iter(matches), None)
        else:
            file_id = resolve_file_id(file_path, node_ids, graph_path.parent)
        if file_id is None:
            hint = (
                " For absolute paths, configure --repo for the indexed repository "
                "or use a repo-relative file path."
                if path.is_absolute()
                else ""
            )
            raise ValueError(f"File not found in selected index: {file_path}.{hint}")
        return data, file_id

    def _file_context_root(self) -> Path:
        """Find repository context without opening or replacing the selected graph."""
        if self.discover:
            for root in [self.base, *self.base.parents]:
                if (root / "blastradius.json").is_file() or (
                    root / ".blastradius/index.db"
                ).is_file():
                    return root
        return self.base

    def symbol_index(self, explicit: str | Path | None = None) -> dict:
        return json.loads(
            self.find("symbolindex.json", explicit).read_text(encoding="utf-8")
        )

    def database(self, explicit: str | Path | None = None):
        from blastradius.store import Store

        store = Store(self.find(".blastradius/index.db", explicit))
        self.callback(store.close)
        return store

    def repository(self, store, explicit: str | Path | None = None) -> Path:
        database = self.find(".blastradius/index.db", explicit)
        if database.parent.name == ".blastradius":
            return database.parent.parent
        root = store.get_meta("repo_root")
        if root and Path(root).is_absolute() and Path(root).is_dir():
            return Path(root).resolve()
        raise ValueError(
            f"Cannot determine the indexed repository for {database}; rebuild its index"
        )


def resolve_file_id(file_path: str, node_ids, root: Path) -> str | None:
    """Prefer exact paths; accept a shortened path only when it is unambiguous."""
    node_ids = set(node_ids)
    path = Path(file_path)
    if path.is_absolute():
        try:
            clean = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return None
    else:
        clean = posixpath.normpath(file_path.replace("\\", "/"))
    if clean in node_ids:
        return clean
    matches = sorted(node for node in node_ids if node.endswith("/" + clean))
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous file {file_path}: {', '.join(matches)}; use a repo-relative path"
        )
    return matches[0] if matches else None


def resolve_symbol_index(explicit: str | None = None) -> dict:
    """CLI symbol-index fallback, independent of the MCP transport."""
    with Artifacts(Path.cwd()) as artifacts:
        return artifacts.symbol_index(explicit)

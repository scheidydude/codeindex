"""Monorepo / workspace detector.

Reads workspace configuration files to identify packages/workspaces within a
monorepo and returns a mapping of file paths → package names.

Supported configs:
  - pnpm-workspace.yaml   (pnpm workspaces)
  - package.json#workspaces (npm / yarn workspaces)
  - lerna.json            (Lerna)
  - nx.json               (Nx)
  - turbo.json            (Turborepo)
  - poetry workspaces (pyproject.toml with [tool.poetry] packages)
"""
import json
import re
from pathlib import Path


def _read_yaml_list(path: Path, key: str):
    """Return list value for 'key' from a simple YAML file (strings only)."""
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(path.read_text(errors="replace"))
        if isinstance(data, dict):
            val = data.get(key, [])
            if isinstance(val, list):
                return [str(v) for v in val]
    except Exception:
        pass
    # Regex fallback — works for simple string lists
    try:
        src = path.read_text(errors="replace")
        # Find lines under the key block
        in_block = False
        results = []
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{key}:"):
                in_block = True
                continue
            if in_block:
                m = re.match(r"^\s*-\s*['\"]?([^'\"#\n]+?)['\"]?\s*$", line)
                if m:
                    results.append(m.group(1).strip())
                elif stripped and not stripped.startswith("-") and not stripped.startswith("#"):
                    in_block = False
        return results
    except Exception:
        return []


def _glob_to_packages(root: Path, globs: list):
    """
    Expand workspace globs (e.g. "packages/*") into a mapping
    package_dir_rel → package_name.
    """
    packages = {}
    for pattern in globs:
        # Normalise glob — workspaces often end with /*, treat as directory search
        if pattern.endswith("/*"):
            base = root / pattern[:-2]
            if base.is_dir():
                for child in base.iterdir():
                    if child.is_dir():
                        pkg_json = child / "package.json"
                        name = child.name
                        if pkg_json.exists():
                            try:
                                data = json.loads(pkg_json.read_text(errors="replace"))
                                name = data.get("name", child.name)
                            except Exception:
                                pass
                        packages[str(child.relative_to(root))] = name
        else:
            candidate = root / pattern
            if candidate.is_dir():
                pkg_json = candidate / "package.json"
                name = candidate.name
                if pkg_json.exists():
                    try:
                        data = json.loads(pkg_json.read_text(errors="replace"))
                        name = data.get("name", candidate.name)
                    except Exception:
                        pass
                packages[str(candidate.relative_to(root))] = name
    return packages


def detect_workspaces(root: Path):
    """
    Return a dict: package_dir_rel → package_name for all detected workspaces.
    Returns empty dict if the repo is not a monorepo.
    """
    packages = {}

    # pnpm-workspace.yaml
    pnpm_ws = root / "pnpm-workspace.yaml"
    if pnpm_ws.exists():
        globs = _read_yaml_list(pnpm_ws, "packages")
        packages.update(_glob_to_packages(root, globs))

    # package.json workspaces (npm / yarn)
    pkg_json = root / "package.json"
    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text(errors="replace"))
            ws = data.get("workspaces", [])
            if isinstance(ws, dict):   # yarn berry: {"packages": [...]}
                ws = ws.get("packages", [])
            if ws:
                packages.update(_glob_to_packages(root, ws))
        except Exception:
            pass

    # lerna.json
    lerna = root / "lerna.json"
    if lerna.exists():
        try:
            data = json.loads(lerna.read_text(errors="replace"))
            ws = data.get("packages", ["packages/*"])
            packages.update(_glob_to_packages(root, ws))
        except Exception:
            pass

    # nx.json — just signals this is an Nx monorepo; packages typically in
    # apps/* and libs/*
    nx = root / "nx.json"
    if nx.exists() and not packages:
        for default_glob in ["apps/*", "libs/*", "packages/*"]:
            packages.update(_glob_to_packages(root, [default_glob]))

    # turbo.json — Turborepo; same default layout
    turbo = root / "turbo.json"
    if turbo.exists() and not packages:
        for default_glob in ["apps/*", "packages/*"]:
            packages.update(_glob_to_packages(root, [default_glob]))

    return packages


def assign_packages(all_nodes: list, workspaces: dict) -> None:
    """
    Mutate each node in-place, adding a ``package`` field.

    The value is the workspace package name if the node lives inside one of
    the detected package directories, otherwise the empty string.
    """
    if not workspaces:
        return
    # Sort by length descending so the most-specific prefix wins
    sorted_ws = sorted(workspaces.items(), key=lambda kv: len(kv[0]), reverse=True)
    for node in all_nodes:
        nid = node.get("id", "")
        for pkg_dir, pkg_name in sorted_ws:
            if nid.startswith(pkg_dir + "/") or nid == pkg_dir:
                node["package"] = pkg_name
                break
        else:
            node.setdefault("package", "")

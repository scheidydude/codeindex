"""Terraform / HCL analyzer.

Produces file-level nodes from .tf files.
Node types:
  "config"  — variables, providers, versions, backend files
  "service" — files defining resources or data sources
  "module"  — files inside a modules/ directory or containing only module calls
Links:
  local module sources, cross-resource references, provider imports.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import dir_group, is_ignored, is_skip_dir, load_gitignore_patterns

# ── Regexes ────────────────────────────────────────────────────────────────────
_RESOURCE_RE = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE)
_DATA_RE = re.compile(r'^data\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE)
_MODULE_RE = re.compile(r'^module\s+"([^"]+)"', re.MULTILINE)
_PROVIDER_RE = re.compile(r'^provider\s+"([^"]+)"', re.MULTILINE)
_SOURCE_ATTR_RE = re.compile(r'\bsource\s*=\s*"([^"]+)"')

# required_providers { provider = { ... } } — one level of nesting
_REQ_PROVIDERS_RE = re.compile(
    r"required_providers\s*\{((?:[^{}]|\{[^{}]*\})*)\}",
    re.DOTALL,
)
# Match provider entries (name = { ... }) but not scalar attrs (name = "...")
_PROV_ENTRY_RE = re.compile(r"^\s+(\w+)\s*=\s*\{", re.MULTILINE)

# Generic resource reference scan: aws_s3_bucket.my_bucket
# Requires at least one underscore AND explicitly excludes var./local./data./module. prefixes
_RES_REF_RE = re.compile(
    r"\b(?!(?:var|local|data|module)\.)([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\.([a-zA-Z0-9_]+)\b"
)
# Data reference: data.type.name
_DATA_REF_RE = re.compile(r"\bdata\.([a-z][a-z0-9_]*)\.([a-zA-Z0-9_]+)\b")
# Variable definition and reference: variable "name" / var.name
_VAR_DEF_RE = re.compile(r'^variable\s+"([^"]+)"', re.MULTILINE)
_VAR_REF_RE = re.compile(r"\bvar\.([a-zA-Z0-9_]+)\b")
# Locals block header — full block extracted via _find_block to handle any nesting depth
_LOCALS_HEADER_RE = re.compile(r"^locals\b", re.MULTILINE)
_LOCAL_KEY_RE = re.compile(r"^\s+(\w+)\s*=", re.MULTILINE)
_LOCAL_NESTED_RE = re.compile(r"\{[^{}]*\}")  # innermost block — applied iteratively
_LOCAL_REF_RE = re.compile(r"\blocal\.([a-zA-Z0-9_]+)\b")
# Output definition
_OUTPUT_DEF_RE = re.compile(r'^output\s+"([^"]+)"', re.MULTILINE)
# Module output reference: module.name.attr — 3-part cross-dir lookup
_MOD_OUTPUT_REF_RE = re.compile(r"\bmodule\.([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)\b")
# Module block reference (2-part): links to file declaring module "name" in same dir
# Negative lookahead excludes 3-part module.X.attr references (handled by _MOD_OUTPUT_REF_RE)
_MOD_REF_RE = re.compile(r"\bmodule\.([a-zA-Z0-9_]+)\b(?!\.[a-zA-Z0-9_])")

_CONFIG_STEMS = {
    "variables",
    "vars",
    "versions",
    "providers",
    "terraform",
    "backend",
    "locals",
    "settings",
    "outputs",
    "output",
}
_MODULE_DIRS = {"modules", "module"}

# Strip common cloud/vendor prefixes when matching module dir name to a file stem
_VENDOR_PREFIX_RE = re.compile(r"^(?:gcp|aws|azure|google|terraform)-")


def collect_files(root: Path, patterns: list) -> list[Path]:
    files = []
    for p in root.rglob("*.tf"):
        if is_skip_dir(p) or is_ignored(p, root, patterns):
            continue
        files.append(p)
    return sorted(files)


def node_type(path: Path, has_resources: bool, has_data: bool) -> str:
    stem = path.stem.lower()
    parts = [p.lower() for p in path.parts]
    if stem in _CONFIG_STEMS:
        return "config"
    if any(p in _MODULE_DIRS for p in parts[:-1]):
        return "module"
    if has_resources or has_data:
        return "service"
    return "module"


def _find_block(source: str, start: int) -> str:
    """Return the content between the braces of the {...} block whose opening brace
    is at the first '{' found at position >= start.  Returns '' if no block found."""
    i = source.find("{", start)
    if i == -1:
        return ""
    depth = 1
    j = i + 1
    n = len(source)
    while j < n and depth > 0:
        c = source[j]
        if c == '"':
            j += 1
            while j < n:
                ch = source[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == '"':
                    break
                j += 1
        elif c == "#" or (c == "/" and j + 1 < n and source[j + 1] == "/"):
            while j < n and source[j] != "\n":
                j += 1
        elif c == "/" and j + 1 < n and source[j + 1] == "*":
            j += 2
            while j + 1 < n and not (source[j] == "*" and source[j + 1] == "/"):
                j += 1
            j += 2
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        j += 1
    return source[i + 1 : j - 1]


def _module_sources(source: str) -> list[tuple[str, str]]:
    """Return (module_name, source_value) for all module blocks in source."""
    results = []
    for m in _MODULE_RE.finditer(source):
        block = _find_block(source, m.end())
        src_m = _SOURCE_ATTR_RE.search(block)
        if src_m:
            results.append((m.group(1), src_m.group(1)))
    return results


def _provider_names(source: str) -> set[str]:
    providers: set[str] = set()
    for m in _PROVIDER_RE.finditer(source):
        providers.add(m.group(1))
    for block in _REQ_PROVIDERS_RE.finditer(source):
        for entry in _PROV_ENTRY_RE.finditer(block.group(1)):
            providers.add(entry.group(1))
    return providers


def _add_link(
    src: str,
    tgt: str,
    seen: set[tuple[str, str]],
    links_map: dict,
) -> None:
    key = (src, tgt)
    if key not in seen:
        seen.add(key)
        links_map[key] = 1


def _group_by_dir(tf_files: list[Path], root: Path) -> dict[str, list[Path]]:
    """Group .tf files by their parent directory (= one Terraform module each)."""
    dirs: dict[str, list[Path]] = {}
    for f in tf_files:
        key = str(f.parent.relative_to(root))
        dirs.setdefault(key, []).append(f)
    return dirs


def analyze(root: Path, group_map: dict):
    patterns = load_gitignore_patterns(root)
    tf_files = collect_files(root, patterns)

    if not tf_files:
        return [], [], {}, {"total_files": 0, "total_loc": 0}

    all_rel = {str(f.relative_to(root)) for f in tf_files}
    # Every directory that contains at least one .tf file is a module entry-point
    module_dirs = {str(f.parent.relative_to(root)) for f in tf_files}

    # Pre-index: module_dir → sorted list of direct .tf file rels in that dir.
    # Used to pick the canonical entry-point file when resolving module sources.
    _dir_files_index: dict[str, list[str]] = {}
    for rel in sorted(all_rel):
        parent = str(Path(rel).parent)
        _dir_files_index.setdefault(parent, []).append(rel)

    def _module_entry(target_dir: str) -> str | None:
        """Return the canonical .tf file to link to for a module directory."""
        candidates = _dir_files_index.get(target_dir, [])
        if not candidates:
            return None
        main = f"{target_dir}/main.tf"
        if main in all_rel:
            return main
        # Try a file whose stem matches the dir name (stripping vendor prefixes)
        dir_stem = _VENDOR_PREFIX_RE.sub("", Path(target_dir).name).replace("-", "_")
        for c in candidates:
            if Path(c).stem == dir_stem:
                return c
        # Prefer any non-config file over variables/outputs/providers/etc.
        for c in candidates:
            if Path(c).stem not in _CONFIG_STEMS:
                return c
        return candidates[0]

    nodes = []
    links_map = {}
    ext_nodes = {}
    total_loc = 0
    global_output_map: dict[tuple[str, str], str] = {}

    # ── Phase 1 (all dirs): parse files, build LOCAL maps, populate global_output_map ──
    # Resources and data sources are scoped to their directory. Two files in
    # different directories may define identically-named resources without
    # any relationship between them.
    per_dir: dict[str, dict] = {}

    for _module_dir, dir_files in _group_by_dir(tf_files, root).items():
        file_data = {}  # rel → metadata
        resource_map = {}  # (type, name) → rel   — scoped to this module dir
        data_map = {}  # (type, name) → rel   — scoped to this module dir
        var_map = {}  # var_name → rel        — scoped to this module dir
        local_map = {}  # local_name → rel      — scoped to this module dir
        module_instance_map = {}  # module_instance_name → rel — scoped to this module dir

        for f in sorted(dir_files):
            rel = str(f.relative_to(root))
            try:
                source = f.read_text(errors="replace")
            except OSError:
                continue

            resources = {
                (m.group(1), m.group(2)) for m in _RESOURCE_RE.finditer(source)
            }
            data_srcs = {(m.group(1), m.group(2)) for m in _DATA_RE.finditer(source)}
            var_defs = {m.group(1) for m in _VAR_DEF_RE.finditer(source)}
            mod_instance = {m.group(1) for m in _MODULE_RE.finditer(source)}
            local_defs: set[str] = set()
            for hm in _LOCALS_HEADER_RE.finditer(source):
                blk = _find_block(source, hm.end())
                flat = blk
                while True:
                    stripped = _LOCAL_NESTED_RE.sub("{}", flat)
                    if stripped == flat:
                        break
                    flat = stripped
                for km in _LOCAL_KEY_RE.finditer(flat):
                    local_defs.add(km.group(1))

            for r in resources:
                resource_map[r] = rel
            for d in data_srcs:
                data_map[d] = rel
            for v in var_defs:
                var_map[v] = rel
            for lv in local_defs:
                local_map[lv] = rel
            for mi in mod_instance:
                module_instance_map[mi] = rel

            file_data[rel] = {
                "source": source,
                "resources": resources,
                "data_sources": data_srcs,
                "modules": _module_sources(source),
                "providers": _provider_names(source),
                "loc": source.count("\n") + 1,
            }

            dir_rel = str(Path(rel).parent)
            for om in _OUTPUT_DEF_RE.finditer(source):
                global_output_map[(dir_rel, om.group(1))] = rel

        # Build module-instance → resolved target dir map for this module dir
        # (used to resolve module.X.Y references to the output file in X's source dir)
        module_src_resolved_map: dict[str, str] = {}
        for rel_tmp, data_tmp in file_data.items():
            f_tmp = root / rel_tmp
            for mod_name, mod_src in data_tmp["modules"]:
                if mod_src.startswith(("./", "../")):
                    try:
                        target_dir = str(
                            (f_tmp.parent / mod_src)
                            .resolve()
                            .relative_to(root.resolve())
                        )
                        module_src_resolved_map[mod_name] = target_dir
                    except ValueError:
                        pass

        per_dir[_module_dir] = {
            "file_data": file_data,
            "resource_map": resource_map,
            "data_map": data_map,
            "var_map": var_map,
            "local_map": local_map,
            "module_instance_map": module_instance_map,
            "module_src_resolved_map": module_src_resolved_map,
        }

    # ── Phase 2 (all dirs): build nodes and edges (global_output_map fully populated) ──
    for d in per_dir.values():
        file_data = d["file_data"]
        resource_map = d["resource_map"]
        data_map = d["data_map"]
        var_map = d["var_map"]
        local_map = d["local_map"]
        module_instance_map = d["module_instance_map"]
        module_src_resolved_map = d["module_src_resolved_map"]

        for rel, data in file_data.items():
            f = root / rel
            loc = data["loc"]
            total_loc += loc
            source = data["source"]

            ntype = node_type(f, bool(data["resources"]), bool(data["data_sources"]))
            seen_links: set[tuple[str, str]] = set()

            # Module source → cross-directory edges (the only legitimate cross-dir link)
            for _mod_name, mod_src in data["modules"]:
                if mod_src.startswith(("./", "../")):
                    try:
                        target_dir = str(
                            (f.parent / mod_src).resolve().relative_to(root.resolve())
                        )
                    except ValueError:
                        continue
                    if target_dir in module_dirs:
                        entry = _module_entry(target_dir)
                        if entry:
                            _add_link(rel, entry, seen_links, links_map)
                else:
                    # Registry module (e.g. "terraform-aws-modules/vpc/aws")
                    ext_key = mod_src.split("//")[0].split("?")[0]
                    ext_nodes.setdefault(
                        ext_key,
                        {
                            "id": ext_key,
                            "type": "import",
                            "language": "terraform",
                            "size": 40,
                            "loc": 0,
                            "group": 9000,
                            "imports": 0,
                        },
                    )
                    _add_link(rel, ext_key, seen_links, links_map)

            # Intra-module resource references — LOCAL map only, no cross-dir leakage
            for m in _RES_REF_RE.finditer(source):
                target_rel = resource_map.get((m.group(1), m.group(2)))
                if target_rel and target_rel != rel:
                    _add_link(rel, target_rel, seen_links, links_map)

            # Intra-module data source references — same scoping rule
            for m in _DATA_REF_RE.finditer(source):
                target_rel = data_map.get((m.group(1), m.group(2)))
                if target_rel and target_rel != rel:
                    _add_link(rel, target_rel, seen_links, links_map)

            # var.name references → link to file defining that variable
            for m in _VAR_REF_RE.finditer(source):
                target_rel = var_map.get(m.group(1))
                if target_rel and target_rel != rel:
                    _add_link(rel, target_rel, seen_links, links_map)

            # local.name references → link to file defining that local
            for m in _LOCAL_REF_RE.finditer(source):
                target_rel = local_map.get(m.group(1))
                if target_rel and target_rel != rel:
                    _add_link(rel, target_rel, seen_links, links_map)

            # module.X.attr — cross-dir link to the file defining output "attr" in X's source
            for m in _MOD_OUTPUT_REF_RE.finditer(source):
                target_dir = module_src_resolved_map.get(m.group(1))
                if target_dir:
                    output_file = global_output_map.get((target_dir, m.group(2)))
                    if output_file and output_file != rel:
                        _add_link(rel, output_file, seen_links, links_map)

            # module.X (2-part) — intra-dir link to the file declaring module "X"
            for m in _MOD_REF_RE.finditer(source):
                target_rel = module_instance_map.get(m.group(1))
                if target_rel and target_rel != rel:
                    _add_link(rel, target_rel, seen_links, links_map)

            # Provider external nodes
            for prov in data["providers"]:
                ext_nodes.setdefault(
                    prov,
                    {
                        "id": prov,
                        "type": "import",
                        "language": "terraform",
                        "size": 40,
                        "loc": 0,
                        "group": 9000,
                        "imports": 0,
                    },
                )
                _add_link(rel, prov, seen_links, links_map)

            nodes.append(
                {
                    "id": rel,
                    "type": ntype,
                    "language": "terraform",
                    "size": loc,
                    "loc": loc,
                    "group": dir_group(f, root, group_map),
                    "imports": len(seen_links),
                }
            )

    return (
        nodes,
        list(ext_nodes.values()),
        links_map,
        {
            "total_files": len(tf_files),
            "total_loc": total_loc,
        },
    )

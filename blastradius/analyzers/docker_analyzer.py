"""Docker / Docker Compose analyzer.

Produces service-level nodes from docker-compose files and Dockerfiles.
Node type: "service"
Links: depends_on relationships between services.
"""
import re
from pathlib import Path

from .base import load_gitignore_patterns, is_ignored, is_skip_dir

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

# ── Compose file names ────────────────────────────────────────────────────────
COMPOSE_NAMES = {
    "docker-compose.yml", "docker-compose.yaml",
    "compose.yml", "compose.yaml",
    "docker-compose.override.yml", "docker-compose.override.yaml",
    "docker-compose.prod.yml", "docker-compose.prod.yaml",
    "docker-compose.dev.yml", "docker-compose.dev.yaml",
}

# Regex fallback for service detection (YAML not available)
_SVC_NAME_RE  = re.compile(r'^  (\w[\w.-]*):\s*$', re.MULTILINE)
_DEPENDS_RE   = re.compile(
    r'depends_on:\s*\n((?:\s*[-\s]*\w[\w.-]*\s*\n)+)', re.MULTILINE
)
_DEP_ITEM_RE  = re.compile(r'[-\s]+(\w[\w.-]+)')

# Dockerfile FROM
_FROM_RE = re.compile(r'^FROM\s+(?:--platform=\S+\s+)?(\S+?)(?:\s+AS\s+(\w+))?$', re.MULTILINE | re.IGNORECASE)


def collect_compose_files(root: Path, patterns: list):
    found = []
    for p in root.rglob("*"):
        if p.name in COMPOSE_NAMES:
            if not is_skip_dir(p) and not is_ignored(p, root, patterns):
                found.append(p)
    return sorted(found)


def collect_dockerfiles(root: Path, patterns: list):
    found = []
    for p in root.rglob("Dockerfile*"):
        if not is_skip_dir(p) and not is_ignored(p, root, patterns):
            found.append(p)
    return sorted(found)


def parse_compose_yaml(source: str):
    """Parse docker-compose YAML; returns dict of service_name → {image, depends_on}."""
    if _HAS_YAML:
        try:
            data = _yaml.safe_load(source)
            if not isinstance(data, dict):
                return {}
            services = data.get("services") or {}
            result = {}
            for name, cfg in services.items():
                if not isinstance(cfg, dict):
                    cfg = {}
                image = cfg.get("image") or (
                    f"build:{cfg['build']['context']}" if isinstance(cfg.get("build"), dict)
                    else ("build:." if cfg.get("build") else None)
                )
                deps_raw = cfg.get("depends_on", [])
                if isinstance(deps_raw, dict):
                    deps = list(deps_raw.keys())
                elif isinstance(deps_raw, list):
                    deps = deps_raw
                else:
                    deps = []
                result[str(name)] = {"image": image, "depends_on": deps}
            return result
        except Exception:
            pass
    # Regex fallback
    result = {}
    for m in _SVC_NAME_RE.finditer(source):
        result[m.group(1)] = {"image": None, "depends_on": []}
    for m in _DEPENDS_RE.finditer(source):
        svc_start = source.rfind("\n  ", 0, m.start())
        # This fallback is imprecise; skip dependency mapping
        _ = [i.group(1) for i in _DEP_ITEM_RE.finditer(m.group(1))]
    return result


def analyze(root: Path, group_map: dict):
    """Returns (nodes, external_nodes, links_map, meta)."""
    patterns      = load_gitignore_patterns(root)
    compose_files = collect_compose_files(root, patterns)
    dockerfiles   = collect_dockerfiles(root, patterns)

    if not compose_files and not dockerfiles:
        return [], [], {}, {"total_files": 0, "total_loc": 0}

    nodes      = []
    links_map  = {}
    ext_images = {}
    total_loc  = 0
    total_files = 0

    for cf in compose_files:
        rel = str(cf.relative_to(root))
        try:
            source = cf.read_text(errors="replace")
        except OSError:
            continue

        total_loc   += source.count("\n") + 1
        total_files += 1

        services = parse_compose_yaml(source)
        if not services:
            continue

        # Compose-file directory as a pseudo-group
        compose_dir = str(cf.parent.relative_to(root)) if cf.parent != root else ""
        group_key   = compose_dir or "."
        if group_key not in group_map:
            group_map[group_key] = len(group_map)
        grp = group_map[group_key]

        # Create one node per service, namespaced by compose file dir
        prefix = compose_dir + "/" if compose_dir else ""

        for svc_name, svc_cfg in services.items():
            node_id = f"{prefix}{svc_name}"
            image   = svc_cfg.get("image") or ""
            nodes.append({
                "id":       node_id,
                "type":     "service",
                "language": "docker",
                "image":    image,
                "size":     60,
                "loc":      0,
                "group":    grp,
                "imports":  len(svc_cfg.get("depends_on", [])),
            })

        # Links from depends_on
        for svc_name, svc_cfg in services.items():
            src_id = f"{prefix}{svc_name}"
            for dep in svc_cfg.get("depends_on", []):
                tgt_id = f"{prefix}{dep}"
                key = (src_id, tgt_id)
                links_map[key] = links_map.get(key, 0) + 1

        # External images as "import" nodes
        for svc_name, svc_cfg in services.items():
            image = svc_cfg.get("image") or ""
            if image and not image.startswith("build:"):
                img_key = image.split(":")[0]  # strip tag
                if img_key not in ext_images:
                    ext_images[img_key] = {
                        "id":       img_key,
                        "type":     "import",
                        "language": "docker",
                        "size":     40,
                        "loc":      0,
                        "group":    9000,
                        "imports":  0,
                    }
                key = (f"{prefix}{svc_name}", img_key)
                links_map[key] = links_map.get(key, 0) + 1

    for df in dockerfiles:
        rel = str(df.relative_to(root))
        try:
            source = df.read_text(errors="replace")
        except OSError:
            continue

        total_loc   += source.count("\n") + 1
        total_files += 1

        # Create a node for the Dockerfile itself
        df_dir    = str(df.parent.relative_to(root)) if df.parent != root else ""
        group_key = df_dir or "."
        if group_key not in group_map:
            group_map[group_key] = len(group_map)

        nodes.append({
            "id":       rel,
            "type":     "service",
            "language": "docker",
            "image":    "",
            "size":     source.count("\n") + 1,
            "loc":      source.count("\n") + 1,
            "group":    group_map[group_key],
            "imports":  0,
        })

        # FROM stages — base images as external imports
        build_stages = {}  # stage alias → True
        for m in _FROM_RE.finditer(source):
            base_img = m.group(1)
            alias    = m.group(2)
            if alias:
                build_stages[alias] = True
            # Skip scratch and local build stages
            if base_img.lower() == "scratch" or base_img in build_stages:
                continue
            img_key = base_img.split(":")[0]
            if img_key not in ext_images:
                ext_images[img_key] = {
                    "id":       img_key,
                    "type":     "import",
                    "language": "docker",
                    "size":     40,
                    "loc":      0,
                    "group":    9000,
                    "imports":  0,
                }
            key = (rel, img_key)
            links_map[key] = links_map.get(key, 0) + 1

    return nodes, list(ext_images.values()), links_map, {
        "total_files": total_files,
        "total_loc":   total_loc,
    }

"""CI/CD pipeline analyzer.

Supports:
  - GitHub Actions (.github/workflows/*.yml / *.yaml)
  - GitLab CI (.gitlab-ci.yml)

Node type: "pipeline" (one node per CI job)
Links: job dependency edges (needs / depends)
"""
import re
from pathlib import Path

from .base import load_gitignore_patterns, is_ignored, is_skip_dir

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


def collect_gha_files(root: Path, patterns: list):
    """Collect GitHub Actions workflow files."""
    workflows_dir = root / ".github" / "workflows"
    if not workflows_dir.exists():
        return []
    files = []
    for p in workflows_dir.glob("*.yml"):
        files.append(p)
    for p in workflows_dir.glob("*.yaml"):
        files.append(p)
    return sorted(files)


def collect_gitlab_files(root: Path):
    for name in (".gitlab-ci.yml", ".gitlab-ci.yaml"):
        p = root / name
        if p.exists():
            return [p]
    return []


def parse_gha_workflow(source: str, rel_path: str):
    """Parse a GitHub Actions workflow file into (nodes, links_map).
    Each job becomes a node; `needs:` creates edges.
    """
    nodes    = []
    links    = {}
    wf_name  = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]  # filename without extension

    if _HAS_YAML:
        try:
            data = _yaml.safe_load(source)
            if not isinstance(data, dict):
                return [], {}
            workflow_label = data.get("name") or wf_name
            jobs = data.get("jobs") or {}
            for job_id, job_cfg in jobs.items():
                if not isinstance(job_cfg, dict):
                    continue
                node_id = f"{wf_name}/{job_id}"
                needs   = job_cfg.get("needs", [])
                if isinstance(needs, str):
                    needs = [needs]
                nodes.append({
                    "id":          node_id,
                    "type":        "pipeline",
                    "language":    "github-actions",
                    "workflow":    workflow_label,
                    "runs_on":     job_cfg.get("runs-on", ""),
                    "size":        50,
                    "loc":         0,
                    "group":       0,   # overridden by caller
                    "imports":     len(needs),
                })
                for dep in needs:
                    dep_id = f"{wf_name}/{dep}"
                    links[(dep_id, node_id)] = 1   # dep must run before job_id
            return nodes, links
        except Exception:
            pass

    # Regex fallback: extract job names from top-level keys
    _JOB_RE   = re.compile(r'^  (\w[\w-]*):\s*$', re.MULTILINE)
    _NEEDS_RE = re.compile(r'needs:\s*\[([^\]]+)\]|needs:\s*(\w[\w-]*)', re.MULTILINE)
    for m in _JOB_RE.finditer(source):
        node_id = f"{wf_name}/{m.group(1)}"
        nodes.append({
            "id": node_id, "type": "pipeline", "language": "github-actions",
            "workflow": wf_name, "runs_on": "", "size": 50, "loc": 0,
            "group": 0, "imports": 0,
        })
    return nodes, links


def parse_gitlab_ci(source: str):
    """Parse a GitLab CI file into (nodes, links_map)."""
    nodes  = []
    links  = {}

    if _HAS_YAML:
        try:
            data = _yaml.safe_load(source)
            if not isinstance(data, dict):
                return [], {}
            # Special keys that are not jobs
            RESERVED = {"stages", "variables", "default", "workflow", "include",
                        "image", "services", "cache", "before_script", "after_script"}
            stages = data.get("stages", [])

            for job_id, job_cfg in data.items():
                if job_id in RESERVED or not isinstance(job_cfg, dict):
                    continue
                if job_id.startswith("."):  # hidden/template jobs
                    continue
                needs_raw = job_cfg.get("needs", [])
                if isinstance(needs_raw, str):
                    needs_raw = [needs_raw]
                deps = []
                for n in needs_raw:
                    if isinstance(n, dict):
                        deps.append(n.get("job", ""))
                    else:
                        deps.append(str(n))
                deps = [d for d in deps if d]

                nodes.append({
                    "id":       job_id,
                    "type":     "pipeline",
                    "language": "gitlab-ci",
                    "stage":    job_cfg.get("stage", ""),
                    "size":     50,
                    "loc":      0,
                    "group":    0,
                    "imports":  len(deps),
                })
                for dep in deps:
                    links[(dep, job_id)] = 1
            return nodes, links
        except Exception:
            pass

    return [], {}


def analyze(root: Path, group_map: dict):
    patterns  = load_gitignore_patterns(root)
    gha_files = collect_gha_files(root, patterns)
    gl_files  = collect_gitlab_files(root)

    if not gha_files and not gl_files:
        return [], [], {}, {"total_files": 0, "total_loc": 0}

    all_nodes  = []
    links_map  = {}
    total_loc  = 0
    total_files = 0

    for f in gha_files:
        rel = str(f.relative_to(root))
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue
        total_loc   += source.count("\n") + 1
        total_files += 1

        # Group by workflow file
        group_key = rel
        if group_key not in group_map:
            group_map[group_key] = len(group_map)
        grp = group_map[group_key]

        nodes, links = parse_gha_workflow(source, rel)
        for n in nodes:
            n["group"] = grp
        all_nodes.extend(nodes)
        for k, v in links.items():
            links_map[k] = links_map.get(k, 0) + v

    for f in gl_files:
        rel = str(f.relative_to(root))
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue
        total_loc   += source.count("\n") + 1
        total_files += 1

        group_key = "gitlab-ci"
        if group_key not in group_map:
            group_map[group_key] = len(group_map)
        grp = group_map[group_key]

        nodes, links = parse_gitlab_ci(source)
        for n in nodes:
            n["group"] = grp
        all_nodes.extend(nodes)
        for k, v in links.items():
            links_map[k] = links_map.get(k, 0) + v

    return all_nodes, [], links_map, {
        "total_files": total_files,
        "total_loc":   total_loc,
    }

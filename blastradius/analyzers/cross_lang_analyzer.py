"""Cross-language API boundary detector.

Matches backend HTTP route definitions against frontend API call sites and
creates "api-call" edges between them.

Backend detection:
  Python — FastAPI (@router.get/post/...), Flask (@app.route), Django path()
  include_router(x.router, prefix="/y") prefix resolution

Frontend detection:
  fetch('/path'), axios.get('/path'), api.get('/path'),
  apiClient.post('/path'), http.get('/path')
"""
import re
from pathlib import Path

# ── Backend route regexes ─────────────────────────────────────────────────────
# FastAPI / Starlette  @router.post("/path")  @app.get("/path")
_FASTAPI_RE = re.compile(
    r"""@\w+\.(get|post|put|delete|patch|options|head)\s*\(\s*['"]([^'"]+)['"]""",
    re.MULTILINE | re.IGNORECASE,
)
# Flask  @app.route("/path")  @bp.route("/path")
_FLASK_RE = re.compile(
    r"""@\w+\.route\s*\(\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)
# Django  path("path/", view)  / re_path(r"^path/$", view)
_DJANGO_RE = re.compile(
    r"""(?:re_)?path\s*\(\s*r?['"]([^'"]+)['"]""",
    re.MULTILINE,
)
# include_router(auth.router, prefix="/auth") → module_alias → prefix
_INCLUDE_ROUTER_RE = re.compile(
    r"""include_router\s*\(\s*(\w+)\.router.*?prefix\s*=\s*['"]([^'"]+)['"]""",
    re.MULTILINE | re.DOTALL,
)
# APIRouter(prefix="/auth") declared in the router file itself
_APIROUTER_PREFIX_RE = re.compile(
    r"""APIRouter\s*\([^)]*prefix\s*=\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)

# ── Frontend API-call regexes ─────────────────────────────────────────────────
# fetch('/path') / fetch(`/path`)
_FETCH_RE = re.compile(r"""fetch\s*\(\s*[`'"](\S+?)[`'"]""", re.MULTILINE)
# axios.get/post/... ('/path')  OR  api.get('/path')  OR  client.post('/path')
_HTTP_CALL_RE = re.compile(
    r"""\b\w+\s*\.\s*(?:get|post|put|delete|patch)\s*\(\s*[`'"]([^`'"]+)[`'"]""",
    re.MULTILINE | re.IGNORECASE,
)


def _normalize(path: str) -> str:
    """Normalise a URL path for fuzzy matching."""
    # Strip query / fragment
    path = path.split("?")[0].split("#")[0]
    # Replace path parameters with <p>
    path = re.sub(r"\{[^}]+\}", "<p>", path)    # {param}
    path = re.sub(r":<[^>]+>", ":<p>", path)     # :<type>
    path = re.sub(r":[A-Za-z_]\w*", "<p>", path) # :param (express style)
    path = re.sub(r"\$\{[^}]+\}", "<p>", path)   # ${var} (template literal)
    path = re.sub(r"<[A-Za-z_][^>]*>", "<p>", path)  # <int:id>
    # Regex anchors (Django)
    path = path.lstrip("^").rstrip("$").rstrip("/")
    return path.lower() or "/"


def _extract_python_routes(source: str):
    """Return list of (method, path) from a Python source file."""
    routes = []
    for m in _FASTAPI_RE.finditer(source):
        routes.append((m.group(1).upper(), m.group(2)))
    for m in _FLASK_RE.finditer(source):
        routes.append(("ANY", m.group(1)))
    for m in _DJANGO_RE.finditer(source):
        p = m.group(1)
        # Skip Django internal patterns
        if not p.startswith("<") and "/" in p or p:
            routes.append(("ANY", p))
    return routes


def _extract_frontend_calls(source: str):
    """Return list of API paths called from a JS/TS file."""
    paths = []
    seen  = set()
    for m in _FETCH_RE.finditer(source):
        p = m.group(1)
        if p not in seen and (p.startswith("/") or p.startswith("http")):
            seen.add(p)
            paths.append(p)
    for m in _HTTP_CALL_RE.finditer(source):
        p = m.group(1)
        if p not in seen and (p.startswith("/") or p.startswith("http")):
            seen.add(p)
            paths.append(p)
    return paths


def _path_matches(call_path: str, route_path: str) -> bool:
    """Check if a frontend call path matches a backend route path."""
    cn = _normalize(call_path)
    rn = _normalize(route_path)
    if cn == rn:
        return True
    # Suffix match: call "/auth/login" matches route "/login"
    if cn.endswith(rn) or rn.endswith(cn):
        return True
    # Segment-level suffix match (at least 1 segment in common)
    c_segs = [s for s in cn.split("/") if s]
    r_segs = [s for s in rn.split("/") if s]
    if not c_segs or not r_segs:
        return False
    # Last N segments match
    overlap = min(len(c_segs), len(r_segs))
    return c_segs[-overlap:] == r_segs[-overlap:]


def find_api_boundaries(root: Path, all_nodes: list) -> list:
    """
    Scan Python and JS/TS source files in the repo, detect route definitions
    and API call sites, and return a list of cross-language link dicts.
    """
    # Build lookup: relative path → node
    node_ids = {n["id"] for n in all_nodes}

    # ── Step 1: collect Python route info ────────────────────────────────────
    # First pass: find include_router calls to build module→prefix map
    include_prefix: dict[str, str] = {}   # module_alias → prefix
    for f in root.rglob("*.py"):
        if any(part in {".venv", "venv", "env", "__pycache__", "node_modules"}
               for part in f.parts):
            continue
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue
        for m in _INCLUDE_ROUTER_RE.finditer(source):
            include_prefix[m.group(1)] = m.group(2)

    # Second pass: collect routes per file with their prefix
    py_routes: dict[str, list[str]] = {}  # rel_path → [full_paths]
    for f in root.rglob("*.py"):
        if any(part in {".venv", "venv", "env", "__pycache__", "node_modules"}
               for part in f.parts):
            continue
        rel = str(f.relative_to(root))
        if rel not in node_ids:
            continue
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue

        routes = _extract_python_routes(source)
        if not routes:
            continue

        # Try to find the prefix for this file
        stem    = f.stem   # e.g., "auth"
        prefix  = include_prefix.get(stem, "")

        # Also check if APIRouter is declared with a prefix in this file
        m = _APIROUTER_PREFIX_RE.search(source)
        if m and not prefix:
            prefix = m.group(1)

        full_paths = [prefix + path for _, path in routes]
        py_routes[rel] = full_paths

    if not py_routes:
        return []

    # ── Step 2: collect JS/TS API calls ──────────────────────────────────────
    JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".vue"}
    js_calls: dict[str, list[str]] = {}
    for f in root.rglob("*"):
        if f.suffix not in JS_EXTS:
            continue
        if any(part in {"node_modules", ".next", "dist", "build"} for part in f.parts):
            continue
        rel = str(f.relative_to(root))
        if rel not in node_ids:
            continue
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue
        calls = _extract_frontend_calls(source)
        if calls:
            js_calls[rel] = calls

    if not js_calls:
        return []

    # ── Step 3: match calls → routes ─────────────────────────────────────────
    # Build inverted index: normalized_path → [py_file]
    route_index: dict[str, list[str]] = {}
    for py_file, paths in py_routes.items():
        for path in paths:
            norm = _normalize(path)
            route_index.setdefault(norm, []).append(py_file)

    new_links = []
    seen_pairs: set[tuple] = set()

    for js_file, calls in js_calls.items():
        for call_path in calls:
            # Skip clearly non-API paths
            if any(call_path.endswith(ext) for ext in (".js", ".css", ".png", ".svg", ".ico")):
                continue

            # Direct normalised match
            call_norm = _normalize(call_path)
            matched_py = route_index.get(call_norm, [])

            # Fallback: suffix/segment match
            if not matched_py:
                for route_norm, py_files in route_index.items():
                    if _path_matches(call_path, route_norm):
                        matched_py = py_files
                        break

            for py_file in matched_py:
                pair = (js_file, py_file)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    new_links.append({
                        "source": js_file,
                        "target": py_file,
                        "weight": 1,
                        "kind":   "api-call",
                    })

    return new_links

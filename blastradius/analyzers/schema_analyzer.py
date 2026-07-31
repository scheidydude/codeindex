"""Database schema analyzer.

Supports:
  - SQL files (*.sql) — parses CREATE TABLE and FOREIGN KEY references
  - Prisma schema (*.prisma / schema.prisma) — parses model definitions and relations
  - SQLAlchemy models are already partially captured by the Python analyzer

Node type: "database"
Links: foreign key / relation edges between tables
"""
import re
from pathlib import Path

from .base import load_gitignore_patterns, is_ignored, is_skip_dir, dir_group

# ── SQL regexes ───────────────────────────────────────────────────────────────
_SQL_TABLE_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
    r'(?:`[^`]+`|"[^"]+"|\'[^\']+\'|\[[\w\s]+\]|(\w+))',
    re.IGNORECASE,
)
# Captures the unquoted name in group 1; also try with backtick/quote variants
_SQL_TABLE_RE2 = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"\[]?(\w+)[`"\]]?',
    re.IGNORECASE,
)
_SQL_FK_RE = re.compile(
    r'(?:FOREIGN\s+KEY\s*\([^)]+\)\s+)?REFERENCES\s+[`"\[]?(\w+)[`"\]]?',
    re.IGNORECASE,
)

# ── Prisma regexes ────────────────────────────────────────────────────────────
_PRISMA_MODEL_RE    = re.compile(r'\bmodel\s+(\w+)\s*\{([^}]+)\}', re.DOTALL)
_PRISMA_FIELD_RE    = re.compile(r'^\s+(\w+)\s+(\w+)', re.MULTILINE)
_PRISMA_RELATION_RE = re.compile(r'@relation\s*\(', re.IGNORECASE)


def collect_sql_files(root: Path, patterns: list):
    files = []
    for p in root.rglob("*.sql"):
        if not is_skip_dir(p) and not is_ignored(p, root, patterns):
            files.append(p)
    return sorted(files)


def collect_prisma_files(root: Path, patterns: list):
    files = []
    for p in root.rglob("*.prisma"):
        if not is_skip_dir(p) and not is_ignored(p, root, patterns):
            files.append(p)
    return sorted(files)


def parse_sql_tables(source: str):
    """Return {table_name: [referenced_table_names]} from a SQL source."""
    tables     = {}
    current    = None

    for m in _SQL_TABLE_RE2.finditer(source):
        current = m.group(1).lower()
        tables[current] = []

    # Extract foreign key references
    # Simple approach: scan whole file for REFERENCES
    fk_matches = _SQL_FK_RE.findall(source)
    # Associate FKs with the most recently declared table — approximate
    # A more accurate approach would track CREATE TABLE blocks, but this works for most schemas
    table_list = list(tables.keys())
    if table_list:
        for ref in fk_matches:
            ref_lower = ref.lower()
            if ref_lower in tables and ref_lower != table_list[-1]:
                tables[table_list[-1]].append(ref_lower)

    return tables


def parse_prisma_models(source: str):
    """Return {model_name: [related_model_names]} from a Prisma schema source."""
    models = {}
    for match in _PRISMA_MODEL_RE.finditer(source):
        name   = match.group(1)
        body   = match.group(2)
        models[name] = []

        # Find fields that reference other models (relation fields)
        for field_m in _PRISMA_FIELD_RE.finditer(body):
            field_type = field_m.group(2).rstrip("?[]")
            # If the field type is another model (capitalized, not a scalar)
            scalars = {"String","Int","Float","Boolean","DateTime","Json",
                       "Bytes","Decimal","BigInt","Unsupported"}
            if field_type not in scalars and field_type[0].isupper():
                models[name].append(field_type)

    return models


def analyze(root: Path, group_map: dict):
    patterns     = load_gitignore_patterns(root)
    sql_files    = collect_sql_files(root, patterns)
    prisma_files = collect_prisma_files(root, patterns)

    if not sql_files and not prisma_files:
        return [], [], {}, {"total_files": 0, "total_loc": 0}

    all_nodes   = []
    links_map   = {}
    total_loc   = 0
    total_files = 0

    # ── SQL ───────────────────────────────────────────────────────────────────
    for f in sql_files:
        rel = str(f.relative_to(root))
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue

        total_loc   += source.count("\n") + 1
        total_files += 1

        tables = parse_sql_tables(source)
        if not tables:
            continue

        grp_key = str(f.parent.relative_to(root)) if f.parent != root else ""
        if grp_key not in group_map:
            group_map[grp_key] = len(group_map)
        grp = group_map[grp_key]

        # Prefix table names with the SQL file path to avoid collisions
        prefix = rel + ":"

        for tbl_name, refs in tables.items():
            node_id = f"{prefix}{tbl_name}"
            all_nodes.append({
                "id":       node_id,
                "type":     "database",
                "language": "sql",
                "size":     55,
                "loc":      0,
                "group":    grp,
                "imports":  len(refs),
            })
            for ref in refs:
                ref_id = f"{prefix}{ref}"
                key    = (node_id, ref_id)
                links_map[key] = links_map.get(key, 0) + 1

    # ── Prisma ────────────────────────────────────────────────────────────────
    for f in prisma_files:
        rel = str(f.relative_to(root))
        try:
            source = f.read_text(errors="replace")
        except OSError:
            continue

        total_loc   += source.count("\n") + 1
        total_files += 1

        models = parse_prisma_models(source)
        if not models:
            continue

        grp_key = str(f.parent.relative_to(root)) if f.parent != root else ""
        if grp_key not in group_map:
            group_map[grp_key] = len(group_map)
        grp = group_map[grp_key]

        prefix = rel + ":"

        for model_name, relations in models.items():
            node_id = f"{prefix}{model_name}"
            all_nodes.append({
                "id":       node_id,
                "type":     "database",
                "language": "prisma",
                "size":     55,
                "loc":      0,
                "group":    grp,
                "imports":  len(set(relations)),
            })
            seen_rels = set()
            for rel_model in relations:
                rel_id = f"{prefix}{rel_model}"
                key    = (node_id, rel_id)
                # Avoid duplicate bidirectional edges
                reverse = (rel_id, node_id)
                if key not in seen_rels and reverse not in links_map:
                    seen_rels.add(key)
                    links_map[key] = 1

    return all_nodes, [], links_map, {
        "total_files": total_files,
        "total_loc":   total_loc,
    }

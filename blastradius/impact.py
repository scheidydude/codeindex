"""Blast-radius computation: direct + transitive dependents + numeric score."""
from __future__ import annotations
from collections import defaultdict, deque


def compute_blast_radius(nodes: list[dict], links: list[dict]) -> dict[str, dict]:
    """
    Returns a mapping of node_id -> {
        direct_dependents: int,
        transitive_dependents: int,
        blast_score: float,
        direct_ids: list[str],
        transitive_ids: list[str],
        dep_paths: dict[str, list[str]],   # target_id -> shortest path from this node
    }

    blast_score = direct_dependents + (0.5 * transitive_dependents)
    """
    node_ids = {n["id"] for n in nodes}

    # Build forward adjacency (who does X import?)
    forward: dict[str, set[str]] = defaultdict(set)
    # Build reverse adjacency (who imports X?)
    reverse: dict[str, set[str]] = defaultdict(set)

    for link in links:
        s, t = link["source"], link["target"]
        if s in node_ids and t in node_ids:
            forward[s].add(t)
            reverse[t].add(s)

    results: dict[str, dict] = {}

    for node in nodes:
        nid = node["id"]
        direct_ids = list(reverse[nid])

        # BFS upward through reverse graph to find all transitive dependents
        visited: set[str] = set()
        queue: deque[tuple[str, list[str]]] = deque()
        dep_paths: dict[str, list[str]] = {}

        for did in direct_ids:
            queue.append((did, [did]))
            visited.add(did)

        while queue:
            current, path = queue.popleft()
            dep_paths[current] = path
            for parent in reverse[current]:
                if parent not in visited and parent != nid:
                    visited.add(parent)
                    queue.append((parent, path + [parent]))

        transitive_ids = [n for n in visited if n not in set(direct_ids)]
        d = len(direct_ids)
        t = len(transitive_ids)

        results[nid] = {
            "direct_dependents":    d,
            "transitive_dependents": t,
            "blast_score":          round(d + 0.5 * t, 2),
            "direct_ids":           direct_ids,
            "transitive_ids":       transitive_ids,
            "dep_paths":            dep_paths,
        }

    return results


def enrich_nodes(nodes: list[dict], blast: dict[str, dict]) -> list[dict]:
    """Attach blast-radius fields to each node in-place."""
    for node in nodes:
        b = blast.get(node["id"], {})
        node["direct_dependents"]    = b.get("direct_dependents", 0)
        node["transitive_dependents"] = b.get("transitive_dependents", 0)
        node["blast_score"]          = b.get("blast_score", 0.0)
    return nodes


def enrich_links(nodes: list[dict], links: list[dict]) -> list[dict]:
    """Add imports / imported_by lists to each node."""
    imports_map:     dict[str, list[str]] = defaultdict(list)
    imported_by_map: dict[str, list[str]] = defaultdict(list)
    node_ids = {n["id"] for n in nodes}

    for link in links:
        s, t = link["source"], link["target"]
        if s in node_ids:
            imports_map[s].append(t)
        if t in node_ids:
            imported_by_map[t].append(s)

    for node in nodes:
        nid = node["id"]
        node["imports"]     = imports_map.get(nid, [])
        node["imported_by"] = imported_by_map.get(nid, [])

    return nodes

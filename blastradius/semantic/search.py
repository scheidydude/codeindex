# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
"""Hybrid query engine: semantic KNN + FTS5 keyword + graph expansion, fused with RRF."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blastradius.semantic.provider import EmbeddingProvider
    from blastradius.store.db import Store

_RRF_K = 60  # standard RRF constant

import re as _re


def _query_stems(query: str) -> list[str]:
    """Return lowercase 4-char stems of each word in the query."""
    return [
        w[:4].lower() for w in _re.sub(r"[^\w\s]", " ", query).split() if len(w) >= 3
    ]


def _path_boost(file_path: str, stems: list[str]) -> float:
    """1.5x if the file path contains any query stem; 1.0 otherwise."""
    path_lower = file_path.lower()
    return 1.5 if any(s in path_lower for s in stems) else 1.0


def _rrf_fuse(ranked_lists: list[list[int]]) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion over lists of symbol IDs.

    score(d) = Σ 1 / (RRF_K + rank_i) across all lists that contain d.
    """
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, sid in enumerate(ranked, start=1):
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (_RRF_K + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def hybrid_search(
    store: Store,
    query: str,
    k: int = 10,
    as_of_reachable: set | None = None,
    provider: EmbeddingProvider | None = None,
) -> list[dict]:
    """Fuse semantic, FTS, and graph signals with Reciprocal Rank Fusion.

    Returns up to *k* result dicts, each with symbol metadata, rrf_score, and signals.
    Degrades gracefully: if provider is None or sqlite-vec absent, uses FTS + graph only.
    If as_of_reachable is provided, restricts to symbols visible at that historical point.
    """
    ranked_lists: list[list[int]] = []
    provenance: dict[int, list[str]] = {}

    def _note(sid: int, signal: str) -> None:
        provenance.setdefault(sid, []).append(signal)

    # 1. Semantic KNN (optional — requires provider + sqlite-vec)
    if provider is not None and store._has_vec:
        try:
            vec = provider.embed([query])[0]
            sem_ids = store.semantic_search(vec, k * 2)
            for sid in sem_ids:
                _note(sid, "semantic")
            if sem_ids:
                ranked_lists.append(sem_ids)
        except Exception as exc:  # noqa: BLE001
            print(f"[blastradius] semantic search skipped: {exc}", file=sys.stderr)
    elif provider is not None and not store._has_vec:
        print(
            "[blastradius] sqlite-vec not available — falling back to FTS + graph search.",
            file=sys.stderr,
        )

    # 2. FTS keyword search
    try:
        fts_ids = store.fts_search(query, k * 2)
        for sid in fts_ids:
            _note(sid, "fts")
        if fts_ids:
            ranked_lists.append(fts_ids)
    except Exception as exc:  # noqa: BLE001
        print(f"[blastradius] FTS search skipped: {exc}", file=sys.stderr)

    # 3. Graph expansion: supplement when primary signals are sparse.
    # Skip if FTS (or semantic) already returned enough results — graph neighbours
    # add structural noise that dilutes good keyword/semantic hits.
    primary_hit_count = sum(len(lst) for lst in ranked_lists)
    if ranked_lists and primary_hit_count < k:
        top_ids: list[int] = []
        for lst in ranked_lists:
            top_ids.extend(lst[:5])
        # deduplicate while preserving order
        seen: set[int] = set()
        deduped: list[int] = []
        for sid in top_ids:
            if sid not in seen:
                seen.add(sid)
                deduped.append(sid)
        graph_ids = store.graph_expand(deduped, k - primary_hit_count)
        for sid in graph_ids:
            if sid not in seen:
                _note(sid, "graph")
        if graph_ids:
            ranked_lists.append(graph_ids)

    if not ranked_lists:
        return []

    fused = _rrf_fuse(ranked_lists)

    # 4. Temporal filter
    if as_of_reachable is not None:
        fused = [
            (sid, score)
            for sid, score in fused
            if store.symbol_visible_at(sid, as_of_reachable)
        ]

    # 5. Hydrate and rerank: boost exported symbols and those whose file path
    # contains query stems (auth.ts ranks above feedback/route.ts for "authentication").
    stems = _query_stems(query)
    hydrated: list[tuple[float, dict]] = []
    for sid, score in fused[: k * 3]:  # oversample so rerank can promote better hits
        sym = store.get_symbol(sid)
        if sym is None:
            continue
        boost = _path_boost(sym["file"], stems)
        if sym.get("exported"):
            boost *= 1.2
        sym["rrf_score"] = round(score * boost, 6)
        sym["signals"] = provenance.get(sid, [])
        hydrated.append((sym["rrf_score"], sym))

    hydrated.sort(key=lambda x: x[0], reverse=True)
    return [sym for _, sym in hydrated[:k]]

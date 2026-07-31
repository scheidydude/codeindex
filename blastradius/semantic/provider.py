# Copyright 2026 David Scheiderman
# Licensed under the Apache License, Version 2.0
"""Embedding provider interface and OpenAI-compatible HTTP client.

Dependency rule: this module must not import from blastradius.store, blastradius.graph,
or any other blastradius layer.  It is a pure provider abstraction.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    @abstractmethod
    def dims(self) -> int: ...


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible /v1/embeddings HTTP client using stdlib urllib only."""

    def __init__(
        self,
        endpoint: str,
        model: str,
        dims: int,
        api_key: str = "",
        batch_size: int = 64,
        timeout: int = 30,
    ) -> None:
        self._endpoint = endpoint.rstrip("/") + "/v1/embeddings"
        self._model = model
        self._dims = dims
        self._api_key = api_key
        self._batch_size = batch_size
        self._timeout = timeout

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            results.extend(self._call(texts[i : i + self._batch_size]))
        return results

    def _call(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self._model, "input": texts}).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(self._endpoint, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"Embedding endpoint unreachable: {exc}") from exc
        data["data"].sort(key=lambda x: x["index"])
        return [item["embedding"] for item in data["data"]]

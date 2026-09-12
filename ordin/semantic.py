from __future__ import annotations

import math

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence


class SemanticReranker(Protocol):
    name: str

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        """Return one bounded similarity-like score per document."""
        ...


@dataclass
class SentenceTransformerReranker:
    """Optional sentence-transformers backend loaded only from a local path."""

    model_path: Path
    _model: Any
    name: str = "sentence-transformers"

    @classmethod
    def from_local_path(cls, model_path: str | Path) -> "SentenceTransformerReranker":
        path = Path(model_path).expanduser().resolve()
        if not path.exists():
            raise ValueError(
                f"semantic model path does not exist: {path}; "
                "Ordin does not download models automatically"
            )
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "semantic reranking requires the optional 'semantic' extra: "
                "pip install 'ordin[semantic]'"
            ) from exc

        # Requiring a filesystem path prevents an accidental Hub/model download.
        model = SentenceTransformer(str(path), local_files_only=True)
        return cls(model_path=path, _model=model)

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        model = self._model
        embeddings = model.encode(
            [query, *documents],
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        query_embedding = embeddings[0]
        document_embeddings = embeddings[1:]
        scores = document_embeddings @ query_embedding
        return [float(value) for value in scores]


def validate_semantic_scores(scores: Sequence[float], expected: int) -> list[float]:
    if len(scores) != expected:
        raise ValueError(
            f"semantic reranker returned {len(scores)} scores for {expected} documents"
        )
    validated: list[float] = []
    for value in scores:
        score = float(value)
        if not math.isfinite(score):
            raise ValueError("semantic reranker scores must be finite")
        # Similarity backends are expected to produce cosine-like values. Clamp
        # rather than letting an optional backend dominate deterministic ranking.
        validated.append(max(-1.0, min(1.0, score)))
    return validated

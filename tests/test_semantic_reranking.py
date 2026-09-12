from pathlib import Path

import pytest

import ordin.search as search_module
from ordin.availability import EnvironmentInfo
from ordin.schema import validate_named_schema
from ordin.search import search
from ordin.semantic import SentenceTransformerReranker, validate_semantic_scores


ENVIRONMENT = EnvironmentInfo(os="linux", distro_id="ubuntu", distro_like=("debian",))


class KeywordReranker:
    name = "fake-keyword"

    def __init__(self, preferred: str):
        self.preferred = preferred
        self.calls = 0
        self.document_counts: list[int] = []

    def score(self, query: str, documents):
        assert query
        self.calls += 1
        self.document_counts.append(len(documents))
        return [1.0 if self.preferred in document else -1.0 for document in documents]


class BrokenReranker:
    name = "broken"

    def score(self, query: str, documents):
        return [0.5]


def _entry(name: str):
    return {
        "schema_version": "ordin.command_card.v1",
        "command": name,
        "summary": "Inspect demo state.",
        "aliases": ["demo inspect"],
        "intents": ["inspect demo state"],
        "default_risk": "low",
        "risk_tags": ["inspection"],
        "examples": [],
        "templates": [],
    }


def _all_installed(command: str) -> str:
    return f"/usr/bin/{command}"


def test_default_search_does_not_invoke_semantic_backend(monkeypatch):
    monkeypatch.setattr(search_module, "load_commands", lambda: [_entry("alpha"), _entry("beta")])
    monkeypatch.setattr(search_module, "load_synonyms", lambda: {})

    result = search(
        "inspect demo state",
        limit=1,
        environment=ENVIRONMENT,
        which=_all_installed,
    )[0]

    assert result.command == "alpha"
    assert result.semantic_reranked is False
    assert result.semantic_score is None


def test_semantic_backend_can_rerank_close_bm25_candidates(monkeypatch):
    monkeypatch.setattr(search_module, "load_commands", lambda: [_entry("alpha"), _entry("beta")])
    monkeypatch.setattr(search_module, "load_synonyms", lambda: {})
    reranker = KeywordReranker("beta")

    result = search(
        "inspect demo state",
        limit=1,
        environment=ENVIRONMENT,
        which=_all_installed,
        semantic_reranker=reranker,
        semantic_weight=1.0,
    )[0]

    assert result.command == "beta"
    assert result.semantic_reranked is True
    assert result.semantic_score == 1.0
    assert "semantic fake-keyword 1.000" in result.why
    assert reranker.calls == 1
    assert reranker.document_counts == [2]


def test_semantic_search_result_remains_schema_valid(monkeypatch):
    monkeypatch.setattr(search_module, "load_commands", lambda: [_entry("alpha"), _entry("beta")])
    monkeypatch.setattr(search_module, "load_synonyms", lambda: {})

    payload = search(
        "inspect demo state",
        limit=1,
        environment=ENVIRONMENT,
        which=_all_installed,
        semantic_reranker=KeywordReranker("beta"),
    )[0].as_dict()

    assert payload["semantic_reranked"] is True
    assert payload["semantic_score"] is not None
    assert validate_named_schema("search_result", payload) == []


def test_semantic_scores_are_clamped_and_length_checked():
    assert validate_semantic_scores([2.0, -5.0, 0.25], 3) == [1.0, -1.0, 0.25]
    with pytest.raises(ValueError, match="scores for"):
        validate_semantic_scores([0.5], 2)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_semantic_scores_cannot_become_confident_ranking_signals(value):
    with pytest.raises(ValueError, match="finite"):
        validate_semantic_scores([value], 1)


def test_optional_backend_loads_an_explicit_local_model_only(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    calls = []
    model = object()

    def constructor(path, *, local_files_only):
        calls.append((path, local_files_only))
        return model

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=constructor)
    )
    result = SentenceTransformerReranker.from_local_path(tmp_path)
    assert calls == [(str(tmp_path.resolve()), True)]
    assert result._model is model


def test_invalid_semantic_weight_is_rejected(monkeypatch):
    monkeypatch.setattr(search_module, "load_commands", lambda: [_entry("alpha")])
    monkeypatch.setattr(search_module, "load_synonyms", lambda: {})

    with pytest.raises(ValueError, match="semantic_weight"):
        search(
            "inspect demo state",
            environment=ENVIRONMENT,
            which=_all_installed,
            semantic_reranker=KeywordReranker("alpha"),
            semantic_weight=1.5,
        )


def test_local_sentence_transformer_backend_refuses_missing_path(tmp_path: Path):
    missing = tmp_path / "not-a-model"
    with pytest.raises(ValueError, match="does not exist"):
        SentenceTransformerReranker.from_local_path(missing)


def test_broken_backend_cannot_return_partial_scores(monkeypatch):
    monkeypatch.setattr(search_module, "load_commands", lambda: [_entry("alpha"), _entry("beta")])
    monkeypatch.setattr(search_module, "load_synonyms", lambda: {})

    with pytest.raises(ValueError, match="returned 1 scores for 2 documents"):
        search(
            "inspect demo state",
            limit=2,
            environment=ENVIRONMENT,
            which=_all_installed,
            semantic_reranker=BrokenReranker(),
        )

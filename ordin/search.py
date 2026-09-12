from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from math import log
from typing import Callable, Literal

from . import SEARCH_SCHEMA_VERSION
from .availability import EnvironmentInfo, command_availability, detect_environment
from .data import load_commands, load_synonyms
from .semantic import SemanticReranker, validate_semantic_scores
from .templates import suggest_commands


TOKEN_RE = re.compile(r"[a-zA-Z0-9_.+-]+")
STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "how",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "this",
    "to",
    "what",
}
WEAK_MATCH_TERMS = {
    "command",
    "directory",
    "directories",
    "file",
    "files",
    "folder",
    "folders",
    "path",
    "paths",
}
BM25_K1 = 1.4
BM25_B = 0.72
BM25_SCALE = 3.0
SEMANTIC_SCALE = 4.0
DEFAULT_SEMANTIC_WEIGHT = 0.5
RankerName = Literal["bm25", "legacy"]


@dataclass(frozen=True)
class SearchResult:
    command: str
    summary: str
    score: float
    why: str
    example: str | None
    risk: str
    matched_terms: list[str]
    suggested_commands: list[dict[str, str]]
    available: bool | None = None
    executable_path: str | None = None
    platform_compatible: bool | None = None
    availability_reason: str | None = None
    semantic_reranked: bool = False
    semantic_score: float | None = None

    def as_dict(self) -> dict:
        return {
            "schema_version": SEARCH_SCHEMA_VERSION,
            "command": self.command,
            "summary": self.summary,
            "score": round(self.score, 3),
            "why": self.why,
            "example": self.example,
            "risk": self.risk,
            "matched_terms": self.matched_terms,
            "suggested_commands": self.suggested_commands,
            "available": self.available,
            "executable_path": self.executable_path,
            "platform_compatible": self.platform_compatible,
            "availability_reason": self.availability_reason,
            "semantic_reranked": self.semantic_reranked,
            "semantic_score": (
                round(self.semantic_score, 4) if self.semantic_score is not None else None
            ),
        }


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text) if token.lower() not in STOPWORDS]


def expand_query(query: str, synonyms: dict[str, list[str]]) -> dict[str, float]:
    tokens = tokenize(query)
    expanded: dict[str, float] = {}
    for token in tokens:
        expanded[token] = expanded.get(token, 0.0) + 2.0
        for synonym in synonyms.get(token, []):
            expanded[synonym] = expanded.get(synonym, 0.0) + 0.8
    return expanded


def command_text(entry: dict) -> str:
    parts = [
        entry.get("command", ""),
        entry.get("summary", ""),
        " ".join(entry.get("intents", [])),
        " ".join(entry.get("aliases", [])),
        " ".join(entry.get("risk_tags", [])),
    ]
    for example in entry.get("examples", []):
        parts.append(example.get("command", ""))
        parts.append(example.get("explanation", ""))
    return " ".join(parts).lower()


def command_tokens(entry: dict) -> Counter[str]:
    return Counter(tokenize(command_text(entry)))


def phrase_match_score(query: str, entry: dict) -> tuple[float, str | None]:
    normalized_query = " ".join(tokenize(query))
    if not normalized_query:
        return 0.0, None
    intents, aliases = _phrase_tokens(entry)
    return _phrase_match_score(normalized_query, intents, aliases)


def _phrase_tokens(entry: dict) -> tuple[list[tuple[str, list[str]]], list[tuple[str, list[str]]]]:
    return (
        [(intent, tokenize(intent)) for intent in entry.get("intents", [])],
        [(alias, tokenize(alias)) for alias in entry.get("aliases", [])],
    )


def _phrase_match_score(
    normalized_query: str,
    intents: list[tuple[str, list[str]]],
    aliases: list[tuple[str, list[str]]],
) -> tuple[float, str | None]:
    if not normalized_query:
        return 0.0, None

    best_score = 0.0
    best_reason: str | None = None
    for intent, tokens in intents:
        normalized_intent = " ".join(tokens)
        if not normalized_intent:
            continue
        if normalized_query == normalized_intent:
            return 8.0, f'exact intent "{intent}"'
        if normalized_query in normalized_intent or normalized_intent in normalized_query:
            score = 4.0
            if score > best_score:
                best_score = score
                best_reason = f'near intent "{intent}"'

    for alias, tokens in aliases:
        normalized_alias = " ".join(tokens)
        if normalized_alias and normalized_alias in normalized_query:
            score = 3.0
            if score > best_score:
                best_score = score
                best_reason = f'alias "{alias}"'

    return best_score, best_reason


def document_frequency(commands: list[dict]) -> Counter[str]:
    frequency: Counter[str] = Counter()
    for entry in commands:
        frequency.update(set(command_tokens(entry)))
    return frequency


def legacy_idf_by_term(commands: list[dict]) -> dict[str, float]:
    return _idf_by_frequency(document_frequency(commands), len(commands), ranker="legacy")


def bm25_idf_by_term(commands: list[dict]) -> dict[str, float]:
    return _idf_by_frequency(document_frequency(commands), len(commands), ranker="bm25")


def _idf_by_frequency(
    frequency: Counter[str], doc_count: int, *, ranker: RankerName
) -> dict[str, float]:
    if ranker == "legacy":
        return {
            term: log((doc_count + 1) / (count + 0.5)) + 1.0 for term, count in frequency.items()
        }
    return {
        term: log(1.0 + (doc_count - count + 0.5) / (count + 0.5))
        for term, count in frequency.items()
    }


def _legacy_lexical_score(
    expanded: dict[str, float],
    token_counts: Counter[str],
    idf: dict[str, float],
) -> float:
    score = 0.0
    for term, weight in expanded.items():
        count = token_counts.get(term, 0)
        if count:
            score += weight * idf.get(term, 1.0) * min(count, 3)
    return score


def _bm25_lexical_score(
    expanded: dict[str, float],
    token_counts: Counter[str],
    idf: dict[str, float],
    *,
    document_length: int,
    average_document_length: float,
) -> float:
    if average_document_length <= 0:
        return 0.0
    length_norm = BM25_K1 * (1.0 - BM25_B + BM25_B * document_length / average_document_length)
    score = 0.0
    for term, query_weight in expanded.items():
        term_frequency = token_counts.get(term, 0)
        if term_frequency <= 0:
            continue
        normalized_tf = term_frequency * (BM25_K1 + 1.0) / (term_frequency + length_norm)
        score += query_weight * idf.get(term, 0.0) * normalized_tf
    return score * BM25_SCALE


def _feature_score(
    query: str,
    entry: dict,
    query_tokens: set[str],
    *,
    normalized_query: str | None = None,
) -> tuple[float, str | None, list[str]]:
    score = 0.0
    signals: list[str] = []
    intents, aliases = _phrase_tokens(entry)
    phrase_score, phrase_reason = _phrase_match_score(
        " ".join(tokenize(query)) if normalized_query is None else normalized_query,
        intents,
        aliases,
    )
    score += phrase_score
    if phrase_reason:
        signals.append(phrase_reason)

    best_intent_overlap = 0
    for _, tokens in intents:
        intent_tokens = set(tokens)
        overlap = intent_tokens & query_tokens
        if overlap:
            score += 2.5 * len(overlap)
            best_intent_overlap = max(best_intent_overlap, len(overlap))
    if best_intent_overlap:
        signals.append(f"intent overlap {best_intent_overlap}")

    alias_overlap_count = 0
    for _, tokens in aliases:
        alias_overlap = set(tokens) & query_tokens
        if alias_overlap:
            score += 2.0 * len(alias_overlap)
            alias_overlap_count = max(alias_overlap_count, len(alias_overlap))
    if alias_overlap_count:
        signals.append(f"alias overlap {alias_overlap_count}")

    command_name = entry.get("command", "").lower()
    if command_name in query_tokens:
        score += 8.0
        signals.append("exact command token")

    return score, phrase_reason, signals


def _semantic_rerank(
    query: str,
    ranked: list[SearchResult],
    documents_by_command: dict[str, str],
    *,
    limit: int,
    semantic_reranker: SemanticReranker,
    semantic_weight: float,
) -> list[SearchResult]:
    if not ranked:
        return []
    if semantic_weight < 0.0 or semantic_weight > 1.0:
        raise ValueError("semantic_weight must be between 0 and 1")

    candidate_count = min(len(ranked), max(limit * 4, 20))
    candidates = ranked[:candidate_count]
    documents = [documents_by_command[item.command] for item in candidates]
    scores = validate_semantic_scores(
        semantic_reranker.score(query, documents),
        len(candidates),
    )

    reranked: list[SearchResult] = []
    for item, semantic_score in zip(candidates, scores):
        adjustment = semantic_score * SEMANTIC_SCALE * semantic_weight
        reranked.append(
            replace(
                item,
                score=item.score + adjustment,
                why=(f"{item.why}; semantic {semantic_reranker.name} {semantic_score:.3f}"),
                semantic_reranked=True,
                semantic_score=semantic_score,
            )
        )
    return sorted(
        reranked,
        key=lambda result: (-result.score, result.command),
    )[:limit]


def _search(
    query: str,
    limit: int,
    *,
    ranker: RankerName,
    environment: EnvironmentInfo | None,
    which: Callable[[str], str | None] | None,
    semantic_reranker: SemanticReranker | None = None,
    semantic_weight: float = DEFAULT_SEMANTIC_WEIGHT,
) -> list[SearchResult]:
    synonyms = load_synonyms()
    expanded = expand_query(query, synonyms)
    commands = load_commands()
    query_words = tokenize(query)
    query_tokens = set(query_words)
    normalized_query = " ".join(query_words)
    environment = environment or detect_environment()

    documents = [command_text(entry) for entry in commands]
    token_counts_by_command = [Counter(tokenize(document)) for document in documents]
    lengths = [sum(counts.values()) for counts in token_counts_by_command]
    average_document_length = sum(lengths) / len(lengths) if lengths else 0.0
    frequency: Counter[str] = Counter()
    for counts in token_counts_by_command:
        frequency.update(counts.keys())
    idf = _idf_by_frequency(frequency, len(commands), ranker=ranker)
    results: list[SearchResult] = []
    for entry, token_counts, document_length in zip(commands, token_counts_by_command, lengths):
        matched_terms = {term for term in expanded if token_counts.get(term, 0) > 0}
        strong_terms = {term for term in matched_terms if term not in WEAK_MATCH_TERMS}
        command_name = entry.get("command", "").lower()
        if command_name and command_name in expanded:
            matched_terms.add(command_name)
            strong_terms.add(command_name)

        feature_score, phrase_reason, feature_signals = _feature_score(
            query, entry, query_tokens, normalized_query=normalized_query
        )
        if ranker == "bm25":
            lexical_score = _bm25_lexical_score(
                expanded,
                token_counts,
                idf,
                document_length=document_length,
                average_document_length=average_document_length,
            )
        else:
            lexical_score = _legacy_lexical_score(expanded, token_counts, idf)

        if command_name in expanded:
            lexical_score += 6.0

        score = lexical_score + feature_score
        if score <= 0 or (phrase_reason is None and not strong_terms):
            continue

        availability = command_availability(
            entry,
            environment=environment,
            which=which,
        )
        score += availability.score_adjustment

        examples = entry.get("examples", [])
        example = examples[0]["command"] if examples else None
        suggested_commands = suggest_commands(entry, query)
        matched = sorted(matched_terms)[:6]
        why_parts = [
            f"{ranker} lexical {lexical_score:.2f}",
            *feature_signals,
        ]
        if matched:
            why_parts.append(f"matched {', '.join(matched)}")
        why_parts.append(availability.reason)
        results.append(
            SearchResult(
                command=entry["command"],
                summary=entry["summary"],
                score=score,
                why="; ".join(why_parts),
                example=example,
                risk=entry.get("default_risk", "unknown"),
                matched_terms=matched,
                suggested_commands=suggested_commands,
                available=availability.installed,
                executable_path=availability.executable_path,
                platform_compatible=availability.platform_compatible,
                availability_reason=availability.reason,
            )
        )

    ranked = sorted(results, key=lambda result: (-result.score, result.command))
    if semantic_reranker is not None:
        if ranker != "bm25":
            raise ValueError("semantic reranking is supported only on the BM25 path")
        documents_by_command = {
            str(entry.get("command", "")): document for entry, document in zip(commands, documents)
        }
        return _semantic_rerank(
            query,
            ranked,
            documents_by_command,
            limit=limit,
            semantic_reranker=semantic_reranker,
            semantic_weight=semantic_weight,
        )
    return ranked[:limit]


def search(
    query: str,
    limit: int = 5,
    *,
    environment: EnvironmentInfo | None = None,
    which: Callable[[str], str | None] | None = None,
    semantic_reranker: SemanticReranker | None = None,
    semantic_weight: float = DEFAULT_SEMANTIC_WEIGHT,
) -> list[SearchResult]:
    return _search(
        query,
        limit,
        ranker="bm25",
        environment=environment,
        which=which,
        semantic_reranker=semantic_reranker,
        semantic_weight=semantic_weight,
    )


def search_legacy(
    query: str,
    limit: int = 5,
    *,
    environment: EnvironmentInfo | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[SearchResult]:
    """Previous weighted-IDF ranker retained for deterministic comparison."""
    return _search(
        query,
        limit,
        ranker="legacy",
        environment=environment,
        which=which,
    )

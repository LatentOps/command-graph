# Deterministic Search Ranking

Ordin uses a deterministic BM25-style lexical ranker for intent-to-command retrieval. The ranker remains local and dependency-free.

Search prepares each command's lexical tokens once per query and reuses them
for document frequency and scoring. Intent and alias tokens are also reused
across phrase and overlap scoring. Command data, enabled packs, and local
executable availability are read again for subsequent queries.

## Ranking Signals

The default search score combines:

1. BM25 lexical relevance over command-card text;
2. exact and near intent matches;
3. intent-token overlap;
4. alias overlap;
5. explicit command-name matches;
6. the bounded local availability/platform signal.

The BM25 component uses command-card document length normalization so commands with many examples or aliases do not receive an unlimited advantage merely because they contain more repeated words.

Synonym-expanded terms retain lower query weight than literal query terms.

## Explainability

The human-readable `why` field identifies the principal signals, including the BM25 lexical contribution, intent/alias evidence, matched terms, and availability/platform evidence.

This is intentionally not a learned black-box ranker.

## Legacy Baseline

The previous weighted-IDF scorer remains available internally as `search_legacy()` for deterministic benchmark comparison. It is not a second user-facing search mode.

The checked-in search-quality benchmark evaluates both rankers under the same injected environment. CI requires the default BM25 ranker to:

- satisfy all fixture-level requirements;
- preserve or improve Recall@K;
- preserve or improve Top-1 accuracy;
- preserve or improve mean reciprocal rank.

This gives later retrieval work a stable baseline rather than relying on anecdotal CLI examples.

## Parameters

The current BM25 constants are checked into `ordin.search`:

- `k1 = 1.4`
- `b = 0.72`
- a fixed lexical scale used only to keep BM25 contribution comparable to the existing explicit feature boosts.

Parameter changes should be justified by the benchmark rather than tuned to one query.

## Later Semantic Reranking

Optional semantic reranking is intentionally separate. The deterministic ranker remains the default retrieval path and must continue to work without model downloads, hosted APIs, or network access.

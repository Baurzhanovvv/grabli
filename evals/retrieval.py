"""Eval A: how well does search find the right journal entry?

Runs several retrieval engines over a real journal and a labeled query set and
reports hit@k, recall@3 and MRR with bootstrap confidence intervals, plus a
paired comparison of every engine against the shipped one.

    uv run --group eval python -m evals.retrieval

The journal and the queries are private and stay under GRABLI_HOME; only the
aggregate numbers this script prints are meant to be published.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from grabli.index import Index, normalize
from grabli.models import Entry
from grabli.store import Store, home

DEPTH = 10  # how far down a ranking is read
BASELINE = "fts5-stem"
_CYRILLIC = re.compile(r"[а-я]")
Ranker = Callable[[str], list[tuple[str, float]]]

# --- metrics ---------------------------------------------------------------


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    return next((1 / place for place, item in enumerate(ranked, start=1) if item in relevant), 0.0)


def hit_at(ranked: list[str], relevant: set[str], k: int) -> float:
    return float(any(item in relevant for item in ranked[:k]))


def recall_at(ranked: list[str], relevant: set[str], k: int) -> float:
    return len(relevant & set(ranked[:k])) / len(relevant)


METRICS: dict[str, Callable[[list[str], set[str]], float]] = {
    "hit@1": lambda ranked, relevant: hit_at(ranked, relevant, 1),
    "hit@3": lambda ranked, relevant: hit_at(ranked, relevant, 3),
    "recall@3": lambda ranked, relevant: recall_at(ranked, relevant, 3),
    "mrr@10": lambda ranked, relevant: reciprocal_rank(ranked[:DEPTH], relevant),
}


def bootstrap(values: list[float], seed: int = 7, rounds: int = 4000) -> tuple[float, float]:
    """95% percentile interval for the mean. Queries are resampled, not entries."""
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(rounds))
    return means[int(0.025 * rounds)], means[int(0.975 * rounds) - 1]


def auc(positive: list[float], negative: list[float]) -> float | None:
    """Chance that a query with an answer scores above one without. 0.5 is a coin flip."""
    if not positive or not negative:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in positive for n in negative)
    return wins / (len(positive) * len(negative))


def fuse(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal rank fusion."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for place, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1 / (k + place)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


# --- engines ---------------------------------------------------------------


def shipped(store: Store) -> Ranker:
    """The search that ships: FTS5, BM25, Snowball stems matched as prefixes."""
    index = Index(store).__enter__()
    return lambda query: [(hit.id, hit.score) for hit in index.search(query, limit=DEPTH)]


def lemmatized(entries: list[Entry]) -> Ranker:
    """Same FTS5 and weights, but Russian words reduced to dictionary forms on both sides."""
    import pymorphy3

    morph = pymorphy3.MorphAnalyzer()
    cache: dict[str, str] = {}

    def lemma(token: str) -> str:
        if not _CYRILLIC.search(token):
            return token
        if token not in cache:
            cache[token] = morph.parse(token)[0].normal_form.replace("ё", "е")
        return cache[token]

    def lemmas(text: str) -> list[str]:
        return [lemma(token) for token in re.findall(r"\w+", normalize(text))]

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE VIRTUAL TABLE fts USING fts5(id UNINDEXED, title, tags, body, "
        "tokenize = 'unicode61 remove_diacritics 2')"
    )
    for entry in entries:
        tags = " ".join([entry.kind, entry.project or "", *entry.stack])
        db.execute(
            "INSERT INTO fts VALUES (?, ?, ?, ?)",
            (
                entry.id,
                " ".join(lemmas(entry.title)),
                " ".join(lemmas(tags)),
                " ".join(lemmas(entry.body)),
            ),
        )

    def rank(query: str) -> list[tuple[str, float]]:
        terms = [t for t in dict.fromkeys(lemmas(query)) if len(t) >= 2][:24]
        if not terms:
            return []
        expression = " OR ".join(
            f'"{t}"*' if not _CYRILLIC.search(t) and len(t) >= 4 else f'"{t}"' for t in terms
        )
        rows = db.execute(
            "SELECT id, -bm25(fts, 0.0, 3.0, 2.0, 1.0) FROM fts WHERE fts MATCH ? "
            "ORDER BY 2 DESC LIMIT ?",
            (expression, DEPTH),
        )
        return [(entry_id, score) for entry_id, score in rows]

    return rank


def embedded(entries: list[Entry], model: str) -> Ranker:
    """Dense retrieval: cosine similarity of the query and `title + body`."""
    import numpy as np
    from fastembed import TextEmbedding

    encoder = TextEmbedding(model)
    e5 = "e5" in model  # the e5 family expects these prefixes
    documents = [f"{'passage: ' if e5 else ''}{entry.title}\n{entry.body}" for entry in entries]
    matrix = np.array(list(encoder.embed(documents)))
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    ids = [entry.id for entry in entries]

    def rank(query: str) -> list[tuple[str, float]]:
        vector = np.array(next(iter(encoder.embed([f"{'query: ' if e5 else ''}{query}"]))))
        scores = matrix @ (vector / np.linalg.norm(vector))
        order = np.argsort(-scores)[:DEPTH]
        return [(ids[i], float(scores[i])) for i in order]

    return rank


def hybrid(*rankers: Ranker) -> Ranker:
    return lambda query: fuse([[item for item, _ in ranker(query)] for ranker in rankers])[:DEPTH]


# --- run -------------------------------------------------------------------


def load_queries(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def evaluate(ranker: Ranker, queries: list[dict]) -> dict:
    """Per-query metric values, top scores and timings for one engine."""
    rows = []
    for query in queries:
        started = time.perf_counter()
        ranked = ranker(query["query"])
        elapsed = time.perf_counter() - started
        ids = [item for item, _ in ranked]
        relevant = set(query["relevant"])
        row = {
            "id": query["id"],
            "type": query["type"],
            "ranked": ids,
            "top_score": ranked[0][1] if ranked else None,
            "returned": len(ranked),
            "seconds": elapsed,
        }
        if relevant:
            row.update({name: metric(ids, relevant) for name, metric in METRICS.items()})
        rows.append(row)
    return {"rows": rows}


def summarize(rows: list[dict], scored: bool) -> dict:
    answered = [row for row in rows if "mrr@10" in row]
    empty = [row for row in rows if "mrr@10" not in row]
    summary: dict = {"queries": len(answered)}
    for name in METRICS:
        values = [row[name] for row in answered]
        low, high = bootstrap(values)
        summary[name] = {"mean": statistics.fmean(values) if values else 0.0, "ci95": [low, high]}
    summary["by_type"] = {
        kind: {
            "queries": len(group),
            **{name: statistics.fmean(row[name] for row in group) for name in METRICS},
        }
        for kind in sorted({row["type"] for row in answered})
        if (group := [row for row in answered if row["type"] == kind])
    }
    summary["no_answer"] = {
        "queries": len(empty),
        "returned_something": statistics.fmean(row["returned"] > 0 for row in empty)
        if empty
        else None,
        # Can the top score tell "there is an answer" from "there is none"?
        "auc_top_score": auc(
            [row["top_score"] or 0.0 for row in answered],
            [row["top_score"] or 0.0 for row in empty],
        )
        if scored
        else None,
    }
    summary["ms_per_query"] = 1000 * statistics.median([row["seconds"] for row in rows] or [0.0])
    return summary


def compare(rows: list[dict], base: list[dict]) -> dict:
    """Paired difference against the baseline, query by query."""
    by_id = {row["id"]: row for row in base if "mrr@10" in row}
    result = {}
    for name in ("hit@3", "mrr@10"):
        deltas = [row[name] - by_id[row["id"]][name] for row in rows if row["id"] in by_id]
        low, high = bootstrap(deltas)
        result[name] = {
            "delta": statistics.fmean(deltas) if deltas else 0.0,
            "ci95": [low, high],
            "better": sum(d > 0 for d in deltas),
            "worse": sum(d < 0 for d in deltas),
        }
    return result


def report(results: dict, counts: dict) -> str:
    def cell(metric: dict) -> str:
        return f"{metric['mean']:.2f} [{metric['ci95'][0]:.2f}, {metric['ci95'][1]:.2f}]"

    lines = [
        f"Journal: {counts['entries']} entries. Queries: {counts['answered']} with an answer, "
        f"{counts['empty']} without. Intervals are 95% bootstrap over queries.",
        "",
        "| Engine | hit@1 | hit@3 | recall@3 | MRR@10 | ms/query |",
        "|---|---|---|---|---|---|",
    ]
    for name, result in results.items():
        s = result["summary"]
        lines.append(
            f"| {name} | {cell(s['hit@1'])} | {cell(s['hit@3'])} | {cell(s['recall@3'])} "
            f"| {cell(s['mrr@10'])} | {s['ms_per_query']:.1f} |"
        )
    lines += ["", f"Paired difference against `{BASELINE}`:", ""]
    lines += ["| Engine | Δ hit@3 | Δ MRR@10 | queries better / worse (MRR) |", "|---|---|---|---|"]
    for name, result in results.items():
        if "versus" not in result:
            continue
        v = result["versus"]
        lines.append(
            f"| {name} | {v['hit@3']['delta']:+.2f} [{v['hit@3']['ci95'][0]:+.2f}, "
            f"{v['hit@3']['ci95'][1]:+.2f}] | {v['mrr@10']['delta']:+.2f} "
            f"[{v['mrr@10']['ci95'][0]:+.2f}, {v['mrr@10']['ci95'][1]:+.2f}] "
            f"| {v['mrr@10']['better']} / {v['mrr@10']['worse']} |"
        )
    types = sorted({kind for result in results.values() for kind in result["summary"]["by_type"]})
    lines += ["", "hit@3 by query type:", "", "| Engine | " + " | ".join(types) + " |"]
    lines.append("|---|" + "---|" * len(types))
    for name, result in results.items():
        by_type = result["summary"]["by_type"]
        lines.append(
            f"| {name} | " + " | ".join(f"{by_type[kind]['hit@3']:.2f}" for kind in types) + " |"
        )
    sizes = next(iter(results.values()))["summary"]["by_type"]
    lines.append(
        "| *queries* | " + " | ".join(str(sizes[kind]["queries"]) for kind in types) + " |"
    )
    lines += ["", "Queries with no relevant entry:", ""]
    lines += ["| Engine | returned something | AUC of top score |", "|---|---|---|"]
    for name, result in results.items():
        no = result["summary"]["no_answer"]
        area = "n/a" if no["auc_top_score"] is None else f"{no['auc_top_score']:.2f}"
        some = "n/a" if no["returned_something"] is None else f"{no['returned_something']:.0%}"
        lines.append(f"| {name} | {some} | {area} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--journal", type=Path, default=home())
    parser.add_argument("--queries", type=Path)
    parser.add_argument("--details", type=Path, help="per-query results; private, keep out of git")
    parser.add_argument(
        "--models",
        nargs="*",
        default=[
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            "intfloat/multilingual-e5-large",
        ],
    )
    args = parser.parse_args(argv)
    queries = load_queries(args.queries or args.journal / "eval" / "queries.jsonl")

    with tempfile.TemporaryDirectory() as scratch:
        # A copy, so the eval never writes a cache or a log into the real journal.
        shutil.copytree(args.journal / "entries", Path(scratch) / "entries")
        store = Store(Path(scratch))
        entries, errors = store.load()
        if errors:
            print("\n".join(errors), file=sys.stderr)
        # The shipped search never returns a superseded entry; give every engine the same set.
        entries = [entry for entry in entries if entry.superseded_by is None]
        live = {entry.id for entry in entries}
        for query in queries:
            gone = set(query["relevant"]) - live
            if gone:
                print(
                    f"{query['id']}: relevant ids not in the journal: {sorted(gone)}",
                    file=sys.stderr,
                )
            query["dropped"] = bool(query["relevant"]) and gone == set(query["relevant"])
            query["relevant"] = [item for item in query["relevant"] if item in live]
        # A query whose every answer is gone is not a "no answer" query: leave it out.
        queries = [query for query in queries if not query["dropped"]]

        engines: dict[str, Ranker] = {BASELINE: shipped(store), "fts5-lemma": lemmatized(entries)}
        for model in args.models:
            short = model.split("/")[-1]
            engines[f"emb:{short}"] = embedded(entries, model)
            engines[f"hybrid:stem+{short}"] = hybrid(engines[BASELINE], engines[f"emb:{short}"])
            engines[f"hybrid:lemma+{short}"] = hybrid(
                engines["fts5-lemma"], engines[f"emb:{short}"]
            )

        results = {name: evaluate(ranker, queries) for name, ranker in engines.items()}

    for name, result in results.items():
        result["summary"] = summarize(result["rows"], scored=not name.startswith("hybrid"))
        if name != BASELINE:
            result["versus"] = compare(result["rows"], results[BASELINE]["rows"])
    counts = {
        "entries": len(entries),
        "answered": sum(bool(query["relevant"]) for query in queries),
        "empty": sum(not query["relevant"] for query in queries),
    }
    if args.details:  # first: a report that fails must not cost a 3-minute run
        args.details.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(report(results, counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

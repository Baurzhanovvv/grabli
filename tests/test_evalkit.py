"""The arithmetic of Eval A. The engines need models and a journal; the metrics do not."""

from __future__ import annotations

import pytest

from evals.retrieval import auc, bootstrap, compare, fuse, hit_at, recall_at, reciprocal_rank


def test_rank_metrics():
    ranked = ["a", "b", "c", "d"]
    assert reciprocal_rank(ranked, {"c"}) == pytest.approx(1 / 3)
    assert reciprocal_rank(ranked, {"z"}) == 0.0
    assert (hit_at(ranked, {"c"}, 1), hit_at(ranked, {"c"}, 3)) == (0.0, 1.0)
    assert recall_at(ranked, {"a", "d", "z"}, 3) == pytest.approx(1 / 3)


def test_bootstrap_interval_contains_the_mean_and_is_reproducible():
    values = [1.0] * 30 + [0.0] * 10
    low, high = bootstrap(values)
    assert low < 0.75 < high
    assert (low, high) == bootstrap(values)
    assert bootstrap([1.0] * 5) == (1.0, 1.0)
    assert bootstrap([]) == (0.0, 0.0)


def test_auc_reads_separation_of_scores():
    assert auc([3, 4], [1, 2]) == 1.0
    assert auc([1, 2], [3, 4]) == 0.0
    assert auc([1, 1], [1, 1]) == 0.5
    assert auc([], [1]) is None


def test_fusion_prefers_what_both_rankings_like():
    fused = [item for item, _ in fuse([["a", "b", "c"], ["c", "b", "x"]])]
    assert fused[0] == "b" or fused[0] == "c"
    assert fused.index("b") < fused.index("x")
    assert fused.index("c") < fused.index("a")


def test_compare_is_paired_by_query():
    base = [{"id": "q1", "hit@3": 0.0, "mrr@10": 0.0}, {"id": "q2", "hit@3": 1.0, "mrr@10": 1.0}]
    rows = [{"id": "q1", "hit@3": 1.0, "mrr@10": 0.5}, {"id": "q2", "hit@3": 1.0, "mrr@10": 0.5}]
    result = compare(rows, base)
    assert result["hit@3"]["delta"] == 0.5
    assert (result["mrr@10"]["better"], result["mrr@10"]["worse"]) == (1, 1)


def test_report_survives_a_set_without_no_answer_queries():
    from evals.retrieval import evaluate, report, summarize

    queries = [{"id": "q1", "type": "task", "query": "x", "relevant": ["a"]}]
    rows = evaluate(lambda query: [("a", 1.0)], queries)["rows"]
    results = {"fts5-stem": {"rows": rows, "summary": summarize(rows, scored=True)}}
    assert "n/a" in report(results, {"entries": 1, "answered": 1, "empty": 0})
    assert summarize([], scored=True)["queries"] == 0

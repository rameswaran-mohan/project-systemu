# tests/test_ra11b2_rank_tools_scored.py
"""R-A11b-2 Task 1 — scored ranking primitive for the reuse confidence floor."""
from systemu.runtime.tool_retrieval import rank_tools_scored, rank_tools


def _catalog():
    return [
        {"name": "fetch_weather", "description": "fetch the weather forecast for a city",
         "parameter_names": ["city"]},
        {"name": "send_email", "description": "send an email message to a recipient",
         "parameter_names": ["to", "subject"]},
    ]


def test_returns_score_tool_pairs_highest_first():
    ranked = rank_tools_scored("fetch_weather forecast", _catalog(), k=8)
    assert isinstance(ranked, list)
    assert isinstance(ranked[0], tuple) and len(ranked[0]) == 2
    score0, tool0 = ranked[0]
    assert isinstance(score0, float)
    assert tool0["name"] == "fetch_weather"
    # descending by score
    scores = [s for s, _ in ranked]
    assert scores == sorted(scores, reverse=True)


def test_order_matches_rank_tools():
    q = "fetch_weather forecast"
    cat = _catalog()
    names_scored = [t["name"] for _, t in rank_tools_scored(q, cat, k=8)]
    names_plain = [t["name"] for t in rank_tools(q, cat, k=8)]
    assert names_scored == names_plain


def test_empty_catalog_returns_empty():
    assert rank_tools_scored("x", [], k=8) == []


def test_no_match_scores_zero():
    ranked = rank_tools_scored("wholly unrelated tokens zzz", _catalog(), k=8)
    assert ranked[0][0] == 0.0

"""The report scores a routing source by what it delivered, not by who it agreed with.

Agreement against the caller is circular as a quality measure: it asks whether Jev
picked what the caller already picked. Delivery is the label that can actually be
wrong, and the outcome records are what carry it.
"""

from __future__ import annotations

from jev.metrics import delivery_by_source, format_report, summarize


def _dispatch(dispatch_id: str, source: str, *, agreed: bool = True, confidence: float = 0.9) -> dict:
    return {
        "ts": "2026-10-10T00:00:00+00:00",
        "pack": "pick_agent",
        "dispatch_id": dispatch_id,
        "source": source,
        "agreed": agreed,
        "jev_confidence": confidence,
    }


def _outcome(dispatch_id: str, delivered: bool) -> dict:
    return {"_type": "outcome", "ts": "2026-10-10T01:00:00+00:00", "dispatch_id": dispatch_id, "delivered": delivered}


def test_delivery_by_source_scores_each_source_separately():
    records = [
        _dispatch("a", "jev"),
        _dispatch("b", "jev"),
        _dispatch("c", "caller"),
        _outcome("a", True),
        _outcome("b", False),
        _outcome("c", True),
    ]
    assert delivery_by_source(records) == {
        "caller": {"dispatches": 1, "delivered": 1, "rate": 1.0},
        "jev": {"dispatches": 2, "delivered": 1, "rate": 0.5},
    }


def test_delivery_counts_a_torn_pair_rather_than_dropping_it():
    assert delivery_by_source([_outcome("orphan", True)]) == {"caller": {"dispatches": 1, "delivered": 1, "rate": 1.0}}


def test_delivery_ignores_an_outcome_with_no_dispatch_id():
    assert delivery_by_source([_outcome("", True), {"_type": "outcome"}]) == {}


def test_delivery_is_empty_without_outcomes():
    assert delivery_by_source([_dispatch("a", "jev")]) == {}


def test_summarize_keeps_outcomes_out_of_the_dispatch_totals():
    summary = summarize([_dispatch("a", "jev", agreed=False), _outcome("a", True)])
    assert summary["total"] == 1
    assert summary["outcomes"] == 1
    assert summary["per_pack"] == {"pick_agent": 1}
    assert summary["agreement"] == 0.0


def test_report_shows_delivery_only_when_outcomes_exist():
    without = format_report([_dispatch("a", "jev")])
    assert "delivery by source" not in without
    with_outcomes = format_report([_dispatch("a", "jev"), _outcome("a", True)])
    assert "delivery by source (1 outcomes):" in with_outcomes
    assert "jev: 1/1 delivered (100%)" in with_outcomes

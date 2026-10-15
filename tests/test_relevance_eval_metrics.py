from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "relevance_eval", Path(__file__).resolve().parents[1] / "scripts" / "relevance_eval.py",
)
assert _SPEC is not None and _SPEC.loader is not None
ev = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ev  # dataclasses resolve annotations via sys.modules
_SPEC.loader.exec_module(ev)

# Hand-computed example: 3 relevant, 3 not.
Y = [1, 1, 0, 1, 0, 0]
P = [0.9, 0.8, 0.7, 0.4, 0.4, 0.1]


def test_roc_auc_hand_computed() -> None:
    # Positive/negative pairs ranked correctly: 0.9 and 0.8 beat all 3 negatives (6),
    # 0.4 beats 0.1 (1) and ties 0.4 (0.5) -> 7.5 / 9.
    assert ev.roc_auc(Y, P) == pytest.approx(7.5 / 9)
    assert ev.roc_auc([1, 1], [0.2, 0.3]) is None


def test_threshold_for_recall_and_confusion() -> None:
    # recall >= 0.9 needs all 3 positives -> lowest positive score 0.4.
    t = ev.threshold_for_recall(Y, P, 0.9)
    assert t == 0.4
    assert ev.confusion_at(Y, P, t) == {"tp": 3, "fp": 2, "fn": 0, "tn": 1}
    precision, recall = ev.precision_recall_at(Y, P, t)
    assert precision == pytest.approx(3 / 5) and recall == 1.0
    # recall >= 0.6 is reached at 0.8 (2 of 3).
    assert ev.threshold_for_recall(Y, P, 0.6) == 0.8


def test_brier_near_half_reliability_percentile() -> None:
    expected = ((0.1**2) + (0.2**2) + (0.7**2) + (0.6**2) + (0.4**2) + (0.1**2)) / 6
    assert ev.brier(Y, P) == pytest.approx(expected)
    assert ev.share_near_half(P) == pytest.approx(2 / 6)
    bins = {b["bin"]: b for b in ev.reliability_by_decile(Y, P)}
    assert bins["0.4-0.5"]["n"] == 2 and bins["0.4-0.5"]["observed"] == 0.5
    assert bins["0.9-1.0"]["n"] == 1
    assert ev.percentile([10, 20, 30, 40], 50) == 25
    assert ev.percentile([], 50) is None


def _arm(name: str, auc: float, cost: float | None, fail: float = 0.0):
    return ev.ArmResult(name, auc, cost, fail)


def test_decide_judge_follows_the_fixed_rule() -> None:
    prior = _arm("prior", 0.70, 0.0)
    haiku = _arm("haiku", 0.85, 0.40)

    assert ev.decide_judge({"prior": prior, "haiku": haiku,
                            "jev": _arm("jev", 0.84, 0.10)})[0] == "jev"
    # too expensive -> haiku
    assert ev.decide_judge({"prior": prior, "haiku": haiku,
                            "jev": _arm("jev", 0.90, 0.30)})[0] == "haiku"
    # too many failures -> haiku
    assert ev.decide_judge({"prior": prior, "haiku": haiku,
                            "jev": _arm("jev", 0.90, 0.10, fail=0.05)})[0] == "haiku"
    # no jev -> haiku if it beats the prior by 0.05
    assert ev.decide_judge({"prior": prior, "haiku": haiku})[0] == "haiku"
    # neither beats the prior -> stop
    assert ev.decide_judge({"prior": prior, "haiku": _arm("haiku", 0.74, 0.4)})[0] == "stop"
    # jev ran without a price -> cannot decide
    assert ev.decide_judge({"prior": prior, "haiku": haiku,
                            "jev": _arm("jev", 0.9, None)})[0] == "undecided"

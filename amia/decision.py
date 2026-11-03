"""Decision engine helpers: composite score, confidence, number-grounding check."""

from __future__ import annotations

import re
from typing import Any

from . import stats
from .config import FRAMEWORK

PRIOR_CONFIDENCE = [(2.5, 0.45), (3.0, 0.50), (3.5, 0.55), (4.01, 0.60)]


def text_score(items: list[dict[str, Any]]) -> float:
    """0-4, neutral 2. Each item contributes (sentiment-2) scaled by materiality and novelty."""
    if not items:
        return 2.0
    signals = [(i["sentiment"] - 2) * (i["materiality"] / 4) * (0.5 + i["novelty"] / 8) for i in items]
    return max(0.0, min(4.0, 2.0 + stats.mean(signals)))


def composite(value: float, quality: float, text: float) -> float:
    f = FRAMEWORK
    return round(f["w_value"] * value + f["w_quality"] * quality + f["w_text"] * text, 4)


def confidence(score: float, calibration: list[tuple[float, float]] | None) -> tuple[float, str]:
    """Probability of beating the equal-weight universe over the decision horizon."""
    if calibration:
        return round(stats.isotonic_predict(calibration, score), 3), "isotonic"
    for upper, p in PRIOR_CONFIDENCE:
        if score < upper:
            return p, "prior"
    return PRIOR_CONFIDENCE[-1][1], "prior"


_NUM = re.compile(r"(?<![\w.])-?\$?\d[\d,]*(?:\.\d+)?%?")
_FORM_NAMES = re.compile(r"\b(?:10-K|10-Q|8-K)\b")
_RANGE = re.compile(r"(\d)-(\d)")


def extract_numbers(text: str) -> list[tuple[str, float, bool]]:
    text = _RANGE.sub(r"\1 \2", _FORM_NAMES.sub(" ", text))
    out = []
    for m in _NUM.finditer(text):
        raw = m.group(0)
        pct = raw.endswith("%")
        val = float(raw.replace("$", "").replace(",", "").rstrip("%"))
        out.append((raw, val, pct))
    return out


def _allowed(facts: dict[str, Any]) -> list[float]:
    vals: list[float] = []

    def walk(v: Any) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            vals.append(float(v))
            if abs(v) <= 5:
                vals.append(float(v) * 100)  # fraction written as a percentage
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(facts)
    return vals


def grounding_check(bullets: list[str], facts: dict[str, Any]) -> tuple[bool, list[str]]:
    """Every number in the thesis must equal a fact, allowing display rounding to 0-2 decimals."""
    allowed = _allowed(facts)
    bad = []
    for b in bullets:
        for raw, val, _ in extract_numbers(b):
            ok = any(abs(round(a, k) - val) < 1e-9 for a in allowed for k in (0, 1, 2)) or any(abs(a - val) < 1e-9 for a in allowed)
            if not ok:
                bad.append(raw)
    return not bad, bad

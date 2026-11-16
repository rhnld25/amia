"""Decision matrix helpers: validate the reasoning model's answer, check its numbers, and a mock stand-in.

The reasoning model decides. Code then checks the answer is well formed and legal for the position,
that every number in the thesis exists in the dossier it was given, and (in pipeline.decide) that the
risk overlay approves it.
"""

from __future__ import annotations

import re
from typing import Any

from .config import DECISION

_NUM = re.compile(r"(?<![\w.])-?\$?\d[\d,]*(?:\.\d+)?%?")
_FORM_NAMES = re.compile(r"\b(?:10-K|10-Q|8-K)\b")
_RANGE = re.compile(r"(\d)-(\d)")
# Structural numbers a thesis may use without them appearing as data (horizons, window names, scales).
_STRUCTURAL = [1, 2, 3, 4, 5, 6, 8, 10, 12, 52, 100, 126, 252]


def extract_numbers(text: str) -> list[tuple[str, float, bool]]:
    text = _RANGE.sub(r"\1 \2", _FORM_NAMES.sub(" ", text))
    out = []
    for m in _NUM.finditer(text):
        raw = m.group(0)
        pct = raw.endswith("%")
        val = float(raw.replace("$", "").replace(",", "").rstrip("%"))
        out.append((raw, val, pct))
    return out


def _allowed(source: Any) -> list[float]:
    vals: list[float] = [float(v) for v in _STRUCTURAL]

    def add(v: float) -> None:
        vals.append(v)
        vals.append(-v)

    def walk(v: Any) -> None:
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            add(float(v))
        elif isinstance(v, str):
            for _, x, _ in extract_numbers(v):
                add(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(source)
    return vals


def grounding_check(bullets: list[str], source: Any) -> tuple[bool, list[str]]:
    """Every number in the thesis must equal a value in `source`, as given or rounded for display."""
    allowed = _allowed(source)
    bad = []
    for b in bullets:
        for raw, val, pct in extract_numbers(b):
            ok = any(abs(round(a, k) - val) < 1e-9 for a in allowed for k in (0, 1, 2, 3, 4))
            if not ok and pct:  # a fraction written as a percentage (0.24 as 24%)
                ok = any(abs(round(a * 100, k) - val) < 1e-9 for a in allowed for k in (0, 1, 2))
            if not ok:
                bad.append(raw)
    return not bad, bad


def _alloc(v: Any) -> float:
    try:
        a = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return a / 100 if a > 1 else a  # a percentage written as a number (2.5 for 2.5%)


def _bullets(v: Any, n: int) -> list[str]:
    return [str(b).strip() for b in (v or []) if str(b).strip()][:n]


def validate_view(d: dict[str, Any], ticker: str) -> tuple[dict[str, Any], list[str]]:
    """Normalize the model's view of one stock. Returns (view, problems); problems mean it can't be used."""
    problems = []
    try:
        p = float(d.get("p_outperform"))
    except (TypeError, ValueError):
        p = float("nan")
    if not 0 <= p <= 1:
        problems.append(f"p_outperform must be between 0 and 1, got {d.get('p_outperform')}")
    alloc = min(max(_alloc(d.get("target_allocation")), 0.0), DECISION["max_position_weight"])
    thesis, counter = _bullets(d.get("thesis"), 3), _bullets(d.get("counter_case"), 2)
    if not thesis:
        problems.append("thesis is empty")
    if not counter:
        problems.append("counter_case is empty")
    views = d.get("framework_views") or {}
    out = {
        "ticker": ticker,
        "p_outperform": round(p, 3) if p == p else None,
        "target_allocation": round(alloc, 4),
        "thesis": thesis,
        "counter_case": counter,
        "framework_views": {k: str(views.get(k, ""))[:400] for k in ("graham", "buffett", "marks")},
        "key_risks": [str(r)[:200] for r in (d.get("key_risks") or [])][:3],
        "lessons_applied": [str(r)[:300] for r in (d.get("lessons_applied") or [])][:5],
    }
    return out, problems


def validate_position(d: dict[str, Any], ticker: str, side: str, max_weight: float) -> tuple[dict[str, Any], list[str]]:
    """Normalize the model's call on one open position: HOLD or EXIT for a long, HOLD or COVER for a short."""
    problems = []
    action = d.get("action")
    allowed = ("HOLD", "EXIT") if side == "long" else ("HOLD", "COVER")
    if action not in allowed:
        problems.append(f"{ticker} is a {side} position, so the action must be {' or '.join(allowed)}, not {action}")
    alloc = min(max(_alloc(d.get("target_allocation")), 0.0), max_weight) if action == "HOLD" else 0.0
    reason = _bullets(d.get("reason"), 2)
    if not reason:
        problems.append("reason is empty")
    return {"ticker": ticker, "action": action, "target_allocation": round(alloc, 4), "reason": reason}, problems


def _pct0(v: float) -> str:
    return f"{v * 100:.0f}%"


def fake_view(ds: dict[str, Any]) -> dict[str, Any]:
    """Deterministic stand-in for the reasoning model in mock runs. Uses only dossier numbers."""
    g, b, att = ds.get("graham") or {}, ds.get("buffett") or {}, ds.get("attention") or {}
    stock = (ds.get("marks") or {}).get("stock") or {}
    market = (ds.get("marks") or {}).get("market") or {}
    score, bullets = 0.0, []
    mos = g.get("mos_band_low")
    if mos is not None:
        score += 1 if mos >= 0.25 else 0.5 if mos >= 0 else -1
        bullets.append(f"Graham: price {_pct0(mos)} versus the intrinsic value band low, {g.get('criteria_passed')} of {g.get('criteria_total')} defensive criteria met.")
    else:
        bullets.append("Graham: intrinsic value unavailable from the filings provided.")
    if b.get("criteria_passed") is not None:
        score += 1 if b["criteria_passed"] >= 3 else -0.5 if b["criteria_passed"] <= 1 else 0
        bullets.append(f"Buffett: {b['criteria_passed']} of {b['criteria_total']} quality criteria met.")
    sent = att.get("avg_sentiment")
    if sent is not None:
        score += 1 if sent >= 2.8 else -1 if sent <= 1.2 else 0
    score += 0.5 if stock.get("score", 0) <= 0 else -0.5
    if market.get("temperature") == "hot":
        score -= 0.5
    if any(ds.get("sector", "") in lesson for lesson in ds.get("lessons") or []):
        score -= 0.5
    bullets.append(
        f"News: attention {att.get('attention')} with average sentiment {sent}; Marks pendulum reads {stock.get('label', 'neutral')}."
    )
    p = round(min(0.8, max(0.2, 0.45 + 0.08 * score)), 2)
    return {
        "ticker": ds["ticker"],
        "p_outperform": p,
        "target_allocation": 0.025 if abs(p - 0.5) >= 0.1 else 0.0,
        "thesis": bullets[:3],
        "counter_case": ["Mock counter case: the frameworks can be wrong about this stock."],
        "framework_views": {"graham": "Mock view.", "buffett": "Mock view.", "marks": "Mock view."},
        "key_risks": ["mock view"],
        "lessons_applied": [lesson for lesson in ds.get("lessons") or [] if ds.get("sector", "") in lesson][:1],
    }


def fake_position(case: dict[str, Any]) -> dict[str, Any]:
    """Mock position call: close when today's view has turned against the position."""
    p = case["view"]["p_outperform"]
    side = case["position"]["side"]
    against = p is not None and ((side == "long" and p < 0.45) or (side == "short" and p > 0.55))
    action = ("EXIT" if side == "long" else "COVER") if against else "HOLD"
    return {
        "ticker": case["position"]["ticker"],
        "action": action,
        "target_allocation": 0.0 if against else case["position"]["weight"],
        "reason": [f"Today's view puts the chance of beating the benchmark at {p}."],
    }

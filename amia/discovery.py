"""Component 1: discovery. Which stocks deserve attention today, judged from scored news and filings.

Every scored item adds attention to the tickers it names, more for novel and strategically relevant items,
decaying with age. The daily watchlist is the top names by attention, weighted by their share of it.
Held positions always stay on the list so they keep being reviewed.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from . import stats
from .config import DISCOVERY

_TICKER = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")


def valid_ticker(symbol: str) -> bool:
    return bool(_TICKER.match(symbol))


def keep_news(item: dict[str, Any], always: set[str]) -> list[str]:
    """Tickers this article should be scored for, or [] to drop it as noise."""
    syms = [s for s in item.get("symbols", []) if valid_ticker(s)]
    if not syms:
        return []
    if any(s in always for s in syms):
        return syms
    if len(syms) > DISCOVERY["max_symbols_per_article"]:
        return []
    text = f"{item.get('headline', '')} {item.get('summary', '')}".lower()
    if not any(k in text for k in DISCOVERY["keywords"]):
        return []
    return syms


def _ts(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    if "T" not in s:
        s += "T00:00:00+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def attention_table(items: list[dict[str, Any]], as_of: str) -> dict[str, dict[str, Any]]:
    """items: scored rows with symbols ('|A|B|'), published_at, novelty, sentiment, relevance."""
    now = _ts(f"{as_of}T23:59:59Z")
    hl = DISCOVERY["half_life_days"]
    per: dict[str, list[tuple[float, float, float, float]]] = {}
    for it in items:
        age = max(0.0, (now - _ts(it["published_at"])).total_seconds() / 86400)
        decay = 0.5 ** (age / hl)
        signal = decay * (0.25 + it["relevance"] / 4) * (0.5 + it["novelty"] / 4)
        for s in it["symbols"].strip("|").split("|"):
            if s:
                per.setdefault(s, []).append((signal, it["sentiment"], age, it["relevance"]))
    out = {}
    for s, rows in per.items():
        total = sum(r[0] for r in rows)
        recent = [r[1] for r in rows if r[2] <= 2]
        earlier = [r[1] for r in rows if r[2] > 2]
        out[s] = {
            "attention": round(total, 3),
            "items": len(rows),
            "avg_sentiment": round(sum(r[0] * r[1] for r in rows) / total, 2) if total else None,
            "sentiment_shift": round(stats.mean(recent) - stats.mean(earlier), 2) if recent and earlier else None,
            "max_relevance": max(r[3] for r in rows),
        }
    return out


def build_watchlist(
    table: dict[str, dict[str, Any]],
    held: set[str],
    eligible: set[str] | None = None,
    company: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Top names by attention (above the floor), plus every held name. `eligible` filters out untradable tickers.

    `company` maps a symbol to its SEC CIK. Share classes of one company (GOOG/GOOGL) take one slot: the held
    class if there is one, otherwise the class with the most attention.
    """
    company = company or {}
    ordered = sorted(
        (
            (s, row)
            for s, row in table.items()
            if row["attention"] >= DISCOVERY["min_attention"] and (eligible is None or s in eligible)
        ),
        key=lambda x: (x[0] not in held, -x[1]["attention"], x[0]),
    )
    seen: set[str] = set()
    ranked = []
    for s, row in ordered:
        key = company.get(s) or s
        if key not in seen:
            seen.add(key)
            ranked.append((s, row))
    ranked = sorted(ranked, key=lambda x: (-x[1]["attention"], x[0]))[: DISCOVERY["watchlist_size"]]
    chosen = {s for s, _ in ranked}
    total = sum(row["attention"] for _, row in ranked) or 1.0
    out = [
        {"symbol": s, "rank": i + 1, **row, "weight": round(row["attention"] / total, 4), "held": 1 if s in held else 0}
        for i, (s, row) in enumerate(ranked)
    ]
    for s in sorted(held - chosen):
        row = table.get(s) or {"attention": 0.0, "items": 0, "avg_sentiment": None, "sentiment_shift": None, "max_relevance": None}
        out.append({"symbol": s, "rank": None, **row, "weight": 0.0, "held": 1})
    return out

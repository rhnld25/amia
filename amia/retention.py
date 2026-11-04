"""Keeps the database inside Supabase's free 500 MB.

News is the only part that grows fast (about 1,500 items a day). What is kept:
- Item text for 45 days. Dossiers only quote the last 30 days of news.
- Scores for items that mention a stock that has ever been on the watchlist, forever. Post-mortems and
  attribution read them for as long as a position was held.
- Scores for everything else for 30 days. Attention only looks back 7 days; after that they are noise.
- LLM spend as one row per call for the current and previous month, then one row per day, model and purpose.

Decisions, snapshots, prices, filings facts and the portfolio are never pruned: they are the track record.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from .config import RETENTION
from .db import DB


def _minus(d: str, days: int) -> str:
    return (date.fromisoformat(d) - timedelta(days=days)).isoformat()


def prune(db: DB, as_of: str) -> dict[str, int]:
    out = {}

    cutoff = _minus(as_of, RETENTION["text_body_days"])
    out["text_items"] = int(db.scalar("SELECT COUNT(*) FROM text_items WHERE published_at < :c AND status IN ('scored', 'failed')", {"c": cutoff}) or 0)
    db.execute("DELETE FROM text_items WHERE published_at < :c AND status IN ('scored', 'failed')", {"c": cutoff})

    cutoff = _minus(as_of, RETENTION["unwatched_score_days"])
    watched = {r["symbol"] for r in db.query("SELECT DISTINCT symbol FROM watchlist")}
    watched |= {r["symbol"] for r in db.query("SELECT DISTINCT symbol FROM decisions")}
    old = db.query("SELECT item_id, score_pass, symbols FROM text_scores WHERE published_at < :c", {"c": cutoff})
    drop = [{"i": r["item_id"], "p": r["score_pass"]} for r in old if not watched & set(r["symbols"].strip("|").split("|"))]
    db.executemany("DELETE FROM text_scores WHERE item_id = :i AND score_pass = :p", drop)
    out["text_scores"] = len(drop)

    # previous month stays per call so a month's spend can still be audited after it closes
    keep_from = (date.fromisoformat(as_of).replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()
    rows = db.query("SELECT * FROM llm_spend WHERE ts < :c AND id NOT LIKE 'day:%'", {"c": keep_from})
    if rows:
        agg: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
            lambda: {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "usd": 0.0, "n": 0}
        )
        for r in rows:
            a = agg[(r["ts"][:10], r["model"], r["purpose"])]
            for k in ("input_tokens", "output_tokens", "cache_read", "cache_write"):
                a[k] += r[k] or 0
            a["usd"] += r["usd"] or 0.0
            a["n"] += 1
        existing = {
            r["id"]: r for r in db.query("SELECT * FROM llm_spend WHERE id LIKE 'day:%' AND ts < :c", {"c": keep_from})
        }
        merged = []
        for (day, model, purpose), a in agg.items():
            rid = f"day:{day}:{model}:{purpose}"
            prev = existing.get(rid) or {}
            merged.append(
                {
                    "id": rid,
                    "ts": f"{day}T00:00:00+00:00",
                    "model": model,
                    "purpose": purpose,
                    "input_tokens": a["input_tokens"] + (prev.get("input_tokens") or 0),
                    "output_tokens": a["output_tokens"] + (prev.get("output_tokens") or 0),
                    "cache_read": a["cache_read"] + (prev.get("cache_read") or 0),
                    "cache_write": a["cache_write"] + (prev.get("cache_write") or 0),
                    "batch": a["n"] + (prev.get("batch") or 0),  # number of calls rolled into this row
                    "usd": a["usd"] + (prev.get("usd") or 0.0),
                }
            )
        db.executemany("DELETE FROM llm_spend WHERE id = :i", [{"i": r["id"]} for r in rows])
        db.upsert("llm_spend", merged, ["id"])
    out["llm_spend"] = len(rows)
    return out

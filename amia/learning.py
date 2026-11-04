"""Component 6: post-mortem and learning loop.

Each month the reasoning model reviews every position closed in the month and every buy whose six-month
window resolved behind SPY. It names the cause and, where there is one, the input it over- or under-weighted.
When enough post-mortems agree on the same lens in the same sector, that becomes a permanent lesson, and
lessons are given to the reasoning model with every future decision in that sector.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .config import EVALUATION, LEARNING
from .db import DB
from .evaluation import ReturnPanel, _feature, _regime
from .llm import LLM, BudgetExceeded

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(db: DB, decision_id: str | None) -> dict[str, Any]:
    if not decision_id:
        return {}
    v = db.scalar("SELECT record FROM decisions WHERE decision_id = :i", {"i": decision_id})
    return json.loads(v) if v else {}


def _news_since(db: DB, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT published_at, item_type, novelty, sentiment, relevance, rationale FROM text_scores "
        "WHERE score_pass = 'primary' AND symbols LIKE :s AND published_at > :a AND published_at <= :b "
        "ORDER BY relevance DESC, published_at DESC LIMIT 8",
        {"s": f"%|{symbol}|%", "a": start, "b": f"{end}T23:59:59Z"},
    )
    return rows


def _features_brief(f: dict[str, Any] | None) -> dict[str, Any] | None:
    if not f:
        return None
    keys = ["price", "graham_mos", "graham_passed", "buffett_passed", "roe_avg", "marks_pendulum", "mom_12_1", "attention", "avg_sentiment"]
    return {k: f[k] for k in keys}


def collect_cases(db: DB, panel: ReturnPanel, month_start: str, as_of: str, sector_etf: dict[str, str]) -> list[dict[str, Any]]:
    done = {r["id"] for r in db.query("SELECT id FROM postmortems")}
    cases = []
    for p in db.query("SELECT * FROM closed_positions WHERE closed_date >= :a AND closed_date <= :b", {"a": month_start, "b": as_of}):
        cid = f"closed:{p['id']}"
        if cid in done:
            continue
        etf = sector_etf.get(p["sector"] or "")
        cases.append(
            {
                "id": cid,
                "case_type": "closed_position",
                "symbol": p["symbol"],
                "sector": p["sector"],
                "decision_id": p["decision_id"],
                "original_decision": _record(db, p["decision_id"]),
                "exit_decision": _record(db, p["exit_decision_id"]),
                "outcome": {
                    "opened": p["opened_date"],
                    "closed": p["closed_date"],
                    "return": round(p["ret"], 4),
                    "excess_vs_spy": round(p["excess_ret"], 4) if p["excess_ret"] is not None else None,
                    "excess_vs_sector_etf": round(p["sector_excess_ret"], 4) if p["sector_excess_ret"] is not None else None,
                    "sector_etf": etf,
                    "tags": json.loads(p["tags"] or "[]"),
                },
                "features_at_open": _features_brief(_feature(db, p["symbol"], p["opened_date"])),
                "features_at_close": _features_brief(_feature(db, p["symbol"], p["closed_date"])),
                "regime_flags_open_close": [_regime(db, p["opened_date"]), _regime(db, p["closed_date"])],
                "news_since_open": _news_since(db, p["symbol"], p["opened_date"], p["closed_date"]),
            }
        )
    h = EVALUATION["decision_horizon"]
    for d in db.query("SELECT decision_id, date, symbol FROM decisions WHERE action = 'ACCUMULATE' AND executed = 1"):
        resolved_on = panel.date_after(d["date"], h)
        if not resolved_on or not month_start <= resolved_on <= as_of:
            continue
        cid = f"missed:{d['decision_id']}"
        ex = panel.excess_vs_spy(d["symbol"], d["date"], h)
        if cid in done or ex is None or ex >= 0:
            continue
        rec = _record(db, d["decision_id"])
        cases.append(
            {
                "id": cid,
                "case_type": "missed_six_month_target",
                "symbol": d["symbol"],
                "sector": rec.get("sector"),
                "decision_id": d["decision_id"],
                "original_decision": rec,
                "outcome": {
                    "decided": d["date"],
                    "resolved": resolved_on,
                    "return": round(panel.forward(d["symbol"], d["date"], h) or 0, 4),
                    "excess_vs_spy": round(ex, 4),
                    "tags": [],
                },
                "features_at_open": _features_brief(_feature(db, d["symbol"], d["date"])),
                "features_at_close": _features_brief(_feature(db, d["symbol"], resolved_on)),
                "regime_flags_open_close": [_regime(db, d["date"]), _regime(db, resolved_on)],
                "news_since_open": _news_since(db, d["symbol"], d["date"], resolved_on),
            }
        )
    return cases


def run_postmortems(db: DB, llm: LLM, panel: ReturnPanel, as_of: str, sector_etf: dict[str, str], notes: list[str]) -> int:
    month_start = (date.fromisoformat(as_of).replace(day=1) - timedelta(days=31)).replace(day=1).isoformat()
    cases = collect_cases(db, panel, month_start, as_of, sector_etf)
    n = 0
    for case in cases:
        try:
            pm = llm.postmortem(case)
        except BudgetExceeded as e:
            notes.append(f"post-mortems paused: {e}")
            break
        except Exception as e:
            log.warning("postmortem %s failed: %s", case["id"], e)
            continue
        db.upsert(
            "postmortems",
            [
                {
                    "id": case["id"],
                    "month": as_of[:7],
                    "case_type": case["case_type"],
                    "symbol": case["symbol"],
                    "sector": case.get("sector") or "Unknown",
                    "decision_id": case["decision_id"],
                    "cause": pm["cause"],
                    "lens": pm["lens"],
                    "adjustment": pm["adjustment"],
                    "explanation": pm["explanation"],
                    "lesson": pm["lesson"],
                    "excess_ret": case["outcome"].get("excess_vs_spy"),
                    "record": json.dumps({"case": case, "postmortem": pm}, default=str),
                    "created_at": _now(),
                }
            ],
            ["id"],
            update=False,
        )
        n += 1
    notes.append(f"post-mortems: {n} of {len(cases)} cases reviewed")
    return n


def update_lessons(db: DB, as_of: str) -> list[dict[str, Any]]:
    """Promote agreeing post-mortems to permanent lessons. Contradicted keys (as many weight_up as weight_down) wait."""
    since = (date.fromisoformat(as_of) - timedelta(days=LEARNING["lesson_window_days"])).isoformat()
    rows = db.query(
        "SELECT * FROM postmortems WHERE lens != 'none' AND adjustment != 'none' AND created_at >= :s ORDER BY created_at",
        {"s": since},
    )
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r["sector"], r["lens"], r["adjustment"])].append(r)
    # the same lesson across sectors becomes a market-wide one once it has twice the evidence
    cross: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        cross[("all", r["lens"], r["adjustment"])].append(r)
    for k, items in cross.items():
        if len(items) >= 2 * LEARNING["lesson_min_cases"] and len({i["sector"] for i in items}) >= 2:
            groups[k] = items
    active = {(r["sector"], r["lens"], r["adjustment"]) for r in db.query("SELECT sector, lens, adjustment FROM lessons WHERE status = 'active'")}
    new = []
    for (sector, lens, adj), items in groups.items():
        opposite = groups.get((sector, lens, "weight_up" if adj == "weight_down" else "weight_down"), [])
        if (sector, lens, adj) in active or len(items) < LEARNING["lesson_min_cases"] or len(opposite) >= len(items):
            continue
        guidance = next((i["lesson"] for i in reversed(items) if i["lesson"]), f"{adj.replace('_', ' ')} {lens} in {sector}")
        lesson = {
            "lesson_id": uuid.uuid4().hex[:12],
            "sector": sector,
            "lens": lens,
            "adjustment": adj,
            "guidance": guidance,
            "evidence": json.dumps([i["id"] for i in items]),
            "n_cases": len(items),
            "status": "active",
            "created_month": as_of[:7],
        }
        db.upsert("lessons", [lesson], ["lesson_id"])
        new.append(lesson)
    return new


def lessons_for(db: DB, sector: str) -> list[str]:
    rows = db.query(
        "SELECT sector, lens, adjustment, guidance, n_cases FROM lessons WHERE status = 'active' AND (sector = :s OR sector = 'all') ORDER BY created_month",
        {"s": sector},
    )
    return [f"[{r['sector']} | {r['lens']} {r['adjustment'].replace('_', ' ')} | {r['n_cases']} post-mortems] {r['guidance']}" for r in rows]

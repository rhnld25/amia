"""Case studies: winning buys written up as study material.

Each month, every executed buy that beat both SPY and its sector ETF by a clear margin, either at the
three-month checkpoint or when the position closed, goes to the reasoning model with the saved snapshot of
everything it saw at the time and what happened afterwards. The model separates wins the thesis earned (the
signals it relied on played out) from lucky ones (it made money for a reason the thesis never named), and
writes up which signals did the work. Every number in the write-up must appear in the case data.

Case studies are reading material only; they do not feed lessons.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .config import CASE_STUDIES
from .db import DB
from .decision import grounding_check
from .evaluation import ReturnPanel, _feature, _regime
from .learning import _features_brief, _news_since, _record
from .llm import LLM, BudgetExceeded
from .snapshots import load_snapshot

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot(db: DB, decision_id: str) -> dict[str, Any] | None:
    digest = db.scalar("SELECT snapshot_hash FROM decisions WHERE decision_id = :i", {"i": decision_id})
    return load_snapshot(db, digest) if digest else None


def _case(db: DB, kind: str, cid: str, d: dict[str, Any], rec: dict[str, Any], end: str, outcome: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": cid,
        "checkpoint": kind,
        "symbol": d["symbol"],
        "sector": rec.get("sector"),
        "decision_id": d["decision_id"],
        "decision": rec,
        "inputs_at_decision": _snapshot(db, d["decision_id"]),
        "outcome": outcome,
        "features_at_decision": _features_brief(_feature(db, d["symbol"], d["date"])),
        "features_at_checkpoint": _features_brief(_feature(db, d["symbol"], end)),
        "regime_flags_decision_checkpoint": [_regime(db, d["date"]), _regime(db, end)],
        "news_since_decision": _news_since(db, d["symbol"], d["date"], end),
    }


def collect_cases(db: DB, panel: ReturnPanel, as_of: str, sector_etf: dict[str, str]) -> list[dict[str, Any]]:
    """Winning buys not yet written up: beat SPY and the sector ETF by `min_excess` at the checkpoint or on close."""
    done = {r["id"] for r in db.query("SELECT id FROM case_studies")}
    margin, h = CASE_STUDIES["min_excess"], CASE_STUDIES["checkpoint_days"]
    buys = {d["decision_id"]: d for d in db.query("SELECT decision_id, date, symbol FROM decisions WHERE action = 'ACCUMULATE' AND executed = 1")}
    closed = {p["decision_id"]: p for p in db.query("SELECT * FROM closed_positions WHERE closed_date <= :d", {"d": as_of})}
    cases = []
    for did, d in buys.items():
        rec = _record(db, did)
        etf = sector_etf.get(rec.get("sector") or "")
        p = closed.get(did)
        cid = f"closed:{p['id']}" if p else None
        if p and cid not in done and (p["excess_ret"] or 0) >= margin and (p["sector_excess_ret"] or 0) >= margin:
            outcome = {
                "decided": d["date"],
                "checkpoint_date": p["closed_date"],
                "trading_days_held": len([x for x in panel.calendar if d["date"] < x <= p["closed_date"]]),
                "return": round(p["ret"], 4),
                "excess_vs_spy": round(p["excess_ret"], 4),
                "excess_vs_sector_etf": round(p["sector_excess_ret"], 4),
                "sector_etf": etf,
                "attribution_tags": json.loads(p["tags"] or "[]"),
            }
            cases.append(_case(db, "closed", cid, d, rec, p["closed_date"], outcome))
            continue
        end = panel.date_after(d["date"], h)
        cid = f"3m:{did}"
        if not end or end > as_of or cid in done or (p and p["closed_date"] <= end):
            continue  # closed before the checkpoint: the close is the only checkpoint
        ret, spy = panel.forward(d["symbol"], d["date"], h), panel.forward("SPY", d["date"], h)
        sec = panel.forward(etf, d["date"], h) if etf else None
        if ret is None or spy is None or sec is None or ret - spy < margin or ret - sec < margin:
            continue
        outcome = {
            "decided": d["date"],
            "checkpoint_date": end,
            "trading_days_held": h,
            "return": round(ret, 4),
            "spy_return": round(spy, 4),
            "sector_etf_return": round(sec, 4),
            "excess_vs_spy": round(ret - spy, 4),
            "excess_vs_sector_etf": round(ret - sec, 4),
            "sector_etf": etf,
        }
        cases.append(_case(db, "3m", cid, d, rec, end, outcome))
    return cases


def _texts(cs: dict[str, Any]) -> list[str]:
    out = [cs["title"], cs["setup"], cs["call"], cs["what_happened"], cs["takeaway"]]
    out += [s["evidence"] for s in cs["signals"]]
    return out


def write_case_studies(db: DB, llm: LLM, panel: ReturnPanel, as_of: str, sector_etf: dict[str, str], notes: list[str]) -> int:
    cases = collect_cases(db, panel, as_of, sector_etf)
    n = 0
    for case in cases:
        try:
            cs = llm.case_study(case)
            ok, bad = grounding_check(_texts(cs), case)
            if not ok:
                cs = llm.case_study(case, feedback=f"These numbers are not in the CASE data: {', '.join(bad)}. Use only numbers from it.")
                ok, bad = grounding_check(_texts(cs), case)
        except BudgetExceeded as e:
            notes.append(f"case studies paused: {e}")
            break
        except Exception as e:
            log.warning("case study %s failed: %s", case["id"], e)
            continue
        db.upsert(
            "case_studies",
            [
                {
                    "id": case["id"],
                    "checkpoint": case["checkpoint"],
                    "symbol": case["symbol"],
                    "sector": case.get("sector") or "Unknown",
                    "decision_id": case["decision_id"],
                    "decided": case["outcome"]["decided"],
                    "checkpoint_date": case["outcome"]["checkpoint_date"],
                    "verdict": cs["verdict"],
                    "title": cs["title"],
                    "excess_spy": case["outcome"]["excess_vs_spy"],
                    "excess_sector": case["outcome"]["excess_vs_sector_etf"],
                    "grounded": 1 if ok else 0,
                    "record": json.dumps({"case": case, "study": cs, "ungrounded": bad}, default=str),
                    "created_at": _now(),
                }
            ],
            ["id"],
            update=False,
        )
        n += 1
    notes.append(f"case studies: {n} of {len(cases)} winning buys written up")
    return n

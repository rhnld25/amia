"""Daily orchestration: data -> quality gates -> text scoring -> fills -> scores -> decisions -> evaluation."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import evaluation, risk, stats
from .alerts import Alerter
from .config import BENCHMARKS, EVALUATION, FRAMEWORK, FRAMEWORK_VERSION, RISK, SECTOR_ETFS, Settings
from .data import Providers, get_providers
from .db import DB
from .decision import composite, confidence, grounding_check, text_score
from .frameworks import graham
from .llm import LLM, RUBRIC_VERSION, BudgetExceeded, template_thesis
from .portfolio import metrics, positions, run_day
from .snapshots import save_snapshot

log = logging.getLogger(__name__)

MACRO_SERIES = ["DAAA", "AAA", "BAMLH0A0HYM2", "T10Y2Y", "DFF", "DTB3"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _minus(d: str, days: int) -> str:
    return (date.fromisoformat(d) - timedelta(days=days)).isoformat()


def _pipe(symbols: list[str]) -> str:
    return "|" + "|".join(symbols) + "|"


class Pipeline:
    def __init__(self, settings: Settings, db: DB | None = None, providers: Providers | None = None):
        self.s = settings
        self.db = db or DB(settings.database_url)
        self.db.init_schema()
        self.p = providers or get_providers(settings)
        self.llm = LLM(settings, self.db)
        self.alerts = Alerter(settings)
        self.notes: list[str] = []

    # ------------------------------------------------------------------ data
    def members(self, as_of: str) -> list[dict[str, Any]]:
        month = as_of[:7]
        if not self.db.scalar("SELECT 1 FROM universe WHERE month = :m LIMIT 1", {"m": month}):
            try:
                rows = self.p.universe(month)
                self.db.upsert("universe", [{**r, "month": month} for r in rows], ["month", "symbol"])
                self.notes.append(f"universe refreshed: {len(rows)} members")
            except Exception as e:
                self.alerts.alert("Universe refresh failed", f"{e}. Using the most recent stored membership.")
        latest = self.db.scalar("SELECT MAX(month) FROM universe WHERE month <= :m", {"m": month})
        if not latest:
            raise RuntimeError("No universe membership available")
        return self.db.query("SELECT * FROM universe WHERE month = :m ORDER BY symbol", {"m": latest})

    def update_prices(self, symbols: list[str], as_of: str) -> None:
        have = {r["symbol"]: r["last"] for r in self.db.query("SELECT symbol, MAX(date) AS last FROM prices GROUP BY symbol")}
        groups: dict[str, list[str]] = defaultdict(list)
        for s in symbols:
            groups[have.get(s) or _minus(as_of, 420)].append(s)
        # symbols up to date share a start; group to keep requests few
        for start, syms in groups.items():
            if start >= as_of:
                continue
            bars = self.p.daily_bars(syms, start, as_of)
            rows = [{"symbol": s, **b} for s, bs in bars.items() for b in bs]
            self.db.upsert("prices", rows, ["symbol", "date"])

    def update_macro(self, as_of: str) -> None:
        for series in MACRO_SERIES:
            last = self.db.scalar("SELECT MAX(date) FROM macro WHERE series = :s", {"s": series})
            try:
                obs = self.p.macro(series, last or _minus(as_of, 500), as_of)
            except Exception as e:
                self.alerts.alert(f"FRED fetch failed: {series}", str(e))
                continue
            self.db.upsert("macro", [{"series": series, "date": d, "value": v} for d, v in obs], ["series", "date"])

    def macro_until(self, as_of: str, lookback_days: int = 200) -> dict[str, list[tuple[str, float]]]:
        out: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for r in self.db.query(
            "SELECT series, date, value FROM macro WHERE date <= :d AND date >= :s ORDER BY date",
            {"d": as_of, "s": _minus(as_of, lookback_days)},
        ):
            out[r["series"]].append((r["date"], r["value"]))
        return out

    def scan_filings(self, members: list[dict[str, Any]], as_of: str) -> None:
        since = self.db.get_kv("filings_since") or _minus(as_of, 5)
        known = {r["accn"] for r in self.db.query("SELECT accn FROM filings WHERE filed >= :s", {"s": since})}
        new_rows, stale, errors = [], set(), 0
        for m in members:
            try:
                filings = self.p.recent_filings(m["cik"], since)
            except Exception as e:
                errors += 1
                log.warning("submissions %s failed: %s", m["cik"], e)
                continue
            for f in filings:
                if f["filed"] > as_of or f["accn"] in known:
                    continue
                known.add(f["accn"])
                new_rows.append({**f, "cik": m["cik"], "symbol": m["symbol"], "url": "", "status": "new", "created_at": _now()})
                if f["form"] in ("10-K", "10-Q"):
                    stale.add(m["cik"])
        self.db.upsert("filings", new_rows, ["accn"], update=False)
        for cik in stale:
            self.db.execute("UPDATE facts_status SET fetched_at = NULL WHERE cik = :c", {"c": cik})
        self.db.set_kv("filings_since", _minus(as_of, 3))
        self.notes.append(f"filings: {len(new_rows)} new, {len(stale)} companies with new financials")
        if members and errors / len(members) > 0.1:
            self.alerts.alert("EDGAR submissions errors", f"{errors} of {len(members)} companies failed on {as_of}.")

    def refresh_facts(self, members: list[dict[str, Any]], limit: int) -> int:
        fresh = {r["cik"] for r in self.db.query("SELECT cik FROM facts_status WHERE fetched_at IS NOT NULL")}
        todo = [m["cik"] for m in members if m["cik"] not in fresh][:limit]
        for cik in todo:
            try:
                rows = self.p.company_facts(cik)
                self.db.upsert("facts", rows, ["cik", "concept", "period_end", "accn"], update=False)
                last = max((r["filed"] for r in rows), default=None)
                self.db.upsert("facts_status", [{"cik": cik, "fetched_at": _now(), "last_filed": last, "error": None}], ["cik"])
            except Exception as e:
                log.warning("companyfacts %s failed: %s", cik, e)
                self.db.upsert("facts_status", [{"cik": cik, "fetched_at": None, "last_filed": None, "error": str(e)[:300]}], ["cik"])
        remaining = len([m for m in members if m["cik"] not in fresh]) - len(todo)
        if remaining > 0:
            self.notes.append(f"fundamentals backfill: {remaining} companies still waiting")
        return len(todo)

    # ------------------------------------------------------------------ text
    def ingest_text(self, universe_symbols: set[str], as_of: str, live_clock: bool) -> None:
        items = []
        for f in self.db.query("SELECT * FROM filings WHERE status = 'new' ORDER BY filed DESC LIMIT 150"):
            try:
                body = self.p.filing_text(f["cik"], f["accn"], f["primary_doc"], f["form"])
                items.append(
                    {
                        "item_id": f"f-{f['accn']}",
                        "item_type": f["form"],
                        "symbols": _pipe([f["symbol"]]),
                        "published_at": f["accepted"] or f["filed"],
                        "body": gzip.compress(body.encode()),
                        "status": "pending",
                        "batch_id": None,
                        "recheck": 0,
                        "created_at": _now(),
                    }
                )
                self.db.execute("UPDATE filings SET status = 'fetched' WHERE accn = :a", {"a": f["accn"]})
            except Exception as e:
                log.warning("filing text %s failed: %s", f["accn"], e)
                self.db.execute("UPDATE filings SET status = 'text_failed' WHERE accn = :a", {"a": f["accn"]})

        since = self.db.get_kv("news_since") or f"{_minus(as_of, 2)}T00:00:00Z"
        end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if live_clock else f"{as_of}T23:59:59Z"
        try:
            news = self.p.news(since, end)
        except Exception as e:
            news = []
            self.alerts.alert("News fetch failed", str(e))
        latest = since
        for n in news:
            latest = max(latest, n["created_at"])
            syms = [s for s in n["symbols"] if s in universe_symbols]
            if not syms:
                continue
            body = f"{n['headline']}\n\n{n['summary']}".strip()
            items.append(
                {
                    "item_id": f"n-{n['id']}",
                    "item_type": "news",
                    "symbols": _pipe(syms),
                    "published_at": n["created_at"],
                    "body": gzip.compress(body.encode()),
                    "status": "pending",
                    "batch_id": None,
                    "recheck": 0,
                    "created_at": _now(),
                }
            )
        self.db.set_kv("news_since", latest)
        self.db.upsert("text_items", items, ["item_id"], update=False)
        self.notes.append(f"text items queued: {len(items)}")

    def score_text(self) -> None:
        pending = self.db.query("SELECT * FROM text_items WHERE status IN ('pending', 'retry') ORDER BY published_at LIMIT 2000")
        new_batch = None
        if pending:
            reqs = []
            for it in pending:
                body = gzip.decompress(bytes(it["body"])).decode()
                reqs.append({"custom_id": it["item_id"], "item_type": it["item_type"], "body": body})
                if int(hashlib.sha256(it["item_id"].encode()).hexdigest(), 16) % 20 == 0 and it["status"] == "pending":
                    reqs.append({"custom_id": it["item_id"] + "--r", "item_type": it["item_type"], "body": body})
            try:
                new_batch = self.llm.submit_scoring_batch(reqs)
                self.db.upsert(
                    "llm_batches",
                    [{"batch_id": new_batch, "kind": "scoring", "created_at": _now(), "status": "in_progress", "n_requests": len(reqs)}],
                    ["batch_id"],
                )
                for it in pending:
                    nxt = "submitted" if it["status"] == "pending" else "resubmitted"
                    self.db.execute("UPDATE text_items SET status = :st, batch_id = :b WHERE item_id = :i", {"st": nxt, "b": new_batch, "i": it["item_id"]})
            except BudgetExceeded as e:
                self.alerts.alert("LLM budget cap reached", f"{e}. Text scoring is paused until next month or a higher cap.")
        for b in self.db.query("SELECT batch_id FROM llm_batches WHERE status = 'in_progress'"):
            wait = self.s.batch_wait_minutes if b["batch_id"] == new_batch else 0
            try:
                results = self.llm.collect_batch(b["batch_id"], wait)
            except Exception as e:
                log.warning("collect %s failed: %s", b["batch_id"], e)
                continue
            if results is None:
                self.notes.append(f"batch {b['batch_id']} still running; collected next run")
                continue
            self._store_scores(results)
            self.db.execute("UPDATE llm_batches SET status = 'ended' WHERE batch_id = :b", {"b": b["batch_id"]})

    def _store_scores(self, results: dict[str, dict[str, Any]]) -> None:
        items = {
            r["item_id"]: r
            for r in self.db.query(
                "SELECT item_id, item_type, symbols, published_at, status FROM text_items WHERE item_id IN ("
                + ",".join(f":i{k}" for k in range(len(results)))
                + ")",
                {f"i{k}": cid.removesuffix("--r") for k, cid in enumerate(results)},
            )
        } if results else {}
        rows = []
        for cid, res in results.items():
            item_id = cid.removesuffix("--r")
            it = items.get(item_id)
            if not it:
                continue
            is_recheck = cid.endswith("--r")
            if "error" in res:
                if not is_recheck:
                    status = "retry" if it["status"] == "submitted" else "failed"
                    self.db.execute("UPDATE text_items SET status = :s WHERE item_id = :i", {"s": status, "i": item_id})
                continue
            rows.append(
                {
                    "item_id": item_id,
                    "score_pass": "recheck" if is_recheck else "primary",
                    "item_type": it["item_type"],
                    "symbols": it["symbols"],
                    "published_at": it["published_at"],
                    **{k: res[k] for k in ("novelty", "sentiment", "materiality", "rationale", "citation")},
                    "model": self.s.scoring_model if not self.llm.fake else "fake",
                    "rubric_version": RUBRIC_VERSION,
                    "scored_at": _now(),
                }
            )
            if not is_recheck:
                self.db.execute("UPDATE text_items SET status = 'scored' WHERE item_id = :i", {"i": item_id})
        self.db.upsert("text_scores", rows, ["item_id", "score_pass"])

    # ---------------------------------------------------------------- daily
    def daily(self, as_of: str, facts_limit: int = 200, live_clock: bool = True) -> dict[str, Any]:
        run_id = uuid.uuid4().hex[:12]
        self.notes = []
        self.db.upsert("runs", [{"run_id": run_id, "kind": "daily", "as_of": as_of, "started": _now(), "finished": None, "status": "running", "notes": None}], ["run_id"])
        status = "ok"
        try:
            result = self._daily(as_of, facts_limit, live_clock)
            status = result.get("status", "ok")
            return result
        except Exception as e:
            status = "error"
            self.alerts.alert("Daily run failed", f"{as_of}: {type(e).__name__}: {e}")
            raise
        finally:
            self.db.execute(
                "UPDATE runs SET finished = :f, status = :s, notes = :n WHERE run_id = :r",
                {"f": _now(), "s": status, "n": json.dumps(self.notes + [f"alert: {a}" for a in self.alerts.sent]), "r": run_id},
            )

    def _daily(self, as_of: str, facts_limit: int, live_clock: bool) -> dict[str, Any]:
        members = self.members(as_of)
        symbols = [m["symbol"] for m in members]
        by_symbol = {m["symbol"]: m for m in members}
        self.update_prices(symbols + BENCHMARKS, as_of)
        if not self.db.scalar("SELECT 1 FROM prices WHERE symbol = 'SPY' AND date = :d", {"d": as_of}):
            self.notes.append("market closed or data not yet available; no decisions")
            return {"status": "market_closed"}
        self.update_macro(as_of)
        self.scan_filings(members, as_of)
        self.refresh_facts(members, facts_limit)
        self.ingest_text(set(symbols), as_of, live_clock)
        self.score_text()

        today = {r["symbol"]: r for r in self.db.query("SELECT * FROM prices WHERE date = :d", {"d": as_of})}
        prev_date = self.db.scalar("SELECT MAX(date) FROM prices WHERE symbol = 'SPY' AND date < :d", {"d": as_of})
        prev_close = {r["symbol"]: r["close"] for r in self.db.query("SELECT symbol, close FROM prices WHERE date = :d", {"d": prev_date})} if prev_date else {}

        # quality gates
        missing = [s for s in symbols if s not in today]
        halt = bool(symbols) and len(missing) / len(symbols) > 0.02
        if halt:
            self.alerts.alert("Data gate: missing prices", f"{len(missing)} of {len(symbols)} universe prices missing on {as_of}; decisions halted.")
        jumps = [s for s in symbols if s in today and today[s]["ret"] is not None and abs(today[s]["ret"]) >= 0.5]
        if jumps:
            self.alerts.alert("Data gate: one-day moves of 50% or more", f"{as_of}: {', '.join(jumps)} excluded from decisions today.")
        macro = self.macro_until(as_of)
        aaa_series = macro.get("DAAA") or macro.get("AAA") or []
        if not aaa_series or (date.fromisoformat(as_of) - date.fromisoformat(aaa_series[-1][0])).days > 7:
            self.alerts.alert("Data gate: AAA yield stale", f"Latest AAA observation: {aaa_series[-1] if aaa_series else 'none'}")

        uni_rets = [today[s]["ret"] for s in symbols if s in today and today[s]["ret"] is not None and s not in jumps]
        rf = (macro.get("DTB3") or [(None, 0.0)])[-1][1]
        port = run_day(
            self.db,
            as_of,
            today,
            prev_close,
            {m["symbol"]: m["index_name"] for m in members},
            {m["symbol"]: m["sector"] for m in members},
            today.get("SPY", {}).get("ret"),
            stats.mean(uni_rets) if uni_rets else None,
            rf,
        )
        self.notes.extend(port.get("events", []))

        scored = self.compute_scores(members, as_of, today, aaa_series)
        decisions = [] if halt else self.decide(as_of, scored, by_symbol, today, macro, set(jumps))

        panel = evaluation.ReturnPanel(self.db, _minus(as_of, 420))
        evaluation.update_ic(self.db, panel)
        evaluation.tag_closed_positions(self.db, panel, SECTOR_ETFS)
        self.health_checks(as_of)
        return {"status": "ok", "nav": port.get("nav"), "decisions": decisions, "scored": len(scored)}

    # --------------------------------------------------------------- scores
    def compute_scores(self, members, as_of: str, today, aaa_series) -> dict[str, dict[str, Any]]:
        aaa = aaa_series[-1][1] if aaa_series else None
        facts_by_cik: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in self.db.query("SELECT cik, concept, period_end, value, filed FROM facts WHERE filed <= :d", {"d": as_of}):
            facts_by_cik[r["cik"]].append(r)
        texts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in self.db.query(
            "SELECT item_id, symbols, novelty, sentiment, materiality, published_at FROM text_scores "
            "WHERE score_pass = 'primary' AND published_at >= :a AND published_at <= :b",
            {"a": _minus(as_of, FRAMEWORK["text_lookback_days"]), "b": f"{as_of}T23:59:59Z"},
        ):
            for s in t["symbols"].strip("|").split("|"):
                texts[s].append(t)
        out, rows = {}, []
        for m in members:
            bar = today.get(m["symbol"])
            if not bar or not facts_by_cik.get(m["cik"]) or not aaa:
                continue
            pit = graham.point_in_time(facts_by_cik[m["cik"]], as_of)
            res = graham.evaluate(pit, bar["close"], aaa)
            v, q, t = graham.value_score(res.mos_band_low), graham.quality_score(res), text_score(texts.get(m["symbol"], []))
            comp = composite(v, q, t)
            out[m["symbol"]] = {"graham": res, "pit": pit, "value": v, "quality": q, "text": t, "texts": texts.get(m["symbol"], []), "composite": comp, "price": bar["close"]}
            rows.append(
                {
                    "date": as_of,
                    "symbol": m["symbol"],
                    "composite": comp,
                    "value_score": v,
                    "quality_score": q,
                    "text_score": t,
                    "mos_low": res.mos_band_low,
                    "iv_mid": res.iv_mid,
                    "price": bar["close"],
                    "criteria_passed": res.criteria_passed,
                }
            )
        self.db.upsert("scores", rows, ["date", "symbol"])
        self.notes.append(f"scored {len(rows)} of {len(members)} universe names")
        return out

    # ------------------------------------------------------------ decisions
    def decide(self, as_of, scored, by_symbol, today, macro, excluded: set[str]) -> list[str]:
        f = FRAMEWORK
        held = positions(self.db)
        nav = float(self.db.scalar("SELECT nav FROM nav WHERE date = :d", {"d": as_of}))
        pending = {r["symbol"] for r in self.db.query("SELECT symbol FROM orders WHERE status = 'pending'")}
        rets: dict[str, dict[str, float]] = defaultdict(dict)
        for r in self.db.query("SELECT symbol, date, ret FROM prices WHERE date > :s AND date <= :d", {"s": _minus(as_of, 400), "d": as_of}):
            if r["ret"] is not None:
                rets[r["symbol"]][r["date"]] = r["ret"]
        spy = rets.get("SPY", {})
        state = risk.PortfolioState(
            nav=nav,
            weights={s: p["value"] / nav for s, p in held.items()},
            sectors={s: (by_symbol.get(s) or {}).get("sector") or p["sector"] for s, p in held.items()},
            betas={s: b for s in held if (b := risk.beta(rets.get(s, {}), spy)) is not None},
        )
        regime = risk.regime_flags(macro)
        panel = evaluation.ReturnPanel(self.db, _minus(as_of, 2000))
        calib, n_resolved = evaluation.calibration(self.db, panel)
        vols = [v for s in scored if (v := risk.annual_vol(rets.get(s, {}))) is not None]
        median_vol = sorted(vols)[len(vols) // 2] if vols else None

        planned: list[dict[str, Any]] = []
        for sym, p in held.items():
            sc = scored.get(sym)
            if not sc or sym in pending:
                continue
            g = sc["graham"]
            reasons = []
            if g.iv_mid and sc["price"] >= g.iv_mid:
                reasons.append("price reached intrinsic value midpoint")
            if g.criteria_passed < f["min_criteria_for_pass"] - 1:
                reasons.append("fundamental break: Graham criteria deteriorated")
            if (date.fromisoformat(as_of) - date.fromisoformat(p["opened_date"])).days > f["max_holding_days"]:
                reasons.append("maximum holding period reached")
            if reasons:
                planned.append({"symbol": sym, "action": "EXIT", "weight": 0.0, "reasons": reasons, "threshold": None, "risk": {}})

        candidates = sorted(
            (
                (sym, sc)
                for sym, sc in scored.items()
                if sym not in held and sym not in pending and sym not in excluded and sc["graham"].screen_pass
                and (sc["graham"].mos_band_low or 0) >= f["min_mos_band_low"]
            ),
            key=lambda x: -x[1]["composite"],
        )
        buys = 0
        for sym, sc in candidates:
            if buys >= f["max_new_positions_per_day"]:
                break
            sector = by_symbol[sym]["sector"]
            threshold, why = risk.threshold_for(sector, state, regime)
            if sc["composite"] < threshold:
                continue
            weight = risk.target_weight(risk.annual_vol(rets.get(sym, {})), median_vol)
            sym_beta = risk.beta(rets.get(sym, {}), spy)
            ok, details = risk.check_buy(sym, sector, weight, sym_beta, state, rets)
            details["threshold_reasons"] = why
            if ok:
                planned.append({"symbol": sym, "action": "ACCUMULATE", "weight": weight, "reasons": [], "threshold": threshold, "risk": details})
                state.weights[sym] = weight
                state.sectors[sym] = sector
                if sym_beta is not None:
                    state.betas[sym] = sym_beta
                buys += 1
            else:
                planned.append({"symbol": sym, "action": "SKIP_RISK", "weight": 0.0, "reasons": details["failures"], "threshold": threshold, "risk": details})

        if not planned:
            return []

        snapshot = {
            "as_of": as_of,
            "code_version": self.s.code_version,
            "framework": FRAMEWORK_VERSION,
            "framework_params": FRAMEWORK,
            "risk_params": RISK,
            "rubric_version": RUBRIC_VERSION,
            "models": {"scorer": self.s.scoring_model, "writer": self.s.thesis_model},
            "macro_latest": {k: v[-1] for k, v in macro.items() if v},
            "regime_flags": regime,
            "calibration": {"method": "isotonic" if calib else "prior", "resolved_observations": n_resolved},
            "portfolio": {"nav": nav, "weights": state.weights},
            "inputs": {
                d["symbol"]: {
                    "price_bar": today.get(d["symbol"]),
                    "facts": {c: scored[d["symbol"]]["pit"].get(c, {}) for c in graham.EPS + graham.REVENUE + graham.DIV_PS + graham.DIV_PAID + graham.LTD + graham.SHARES + ["AssetsCurrent", "LiabilitiesCurrent", "StockholdersEquity"]},
                    "text_items": scored[d["symbol"]]["texts"],
                }
                for d in planned
            },
        }
        digest = save_snapshot(self.db, as_of, snapshot)
        resolve_on = (date.fromisoformat(as_of) + timedelta(days=round(EVALUATION["decision_horizon"] * 365 / 252))).isoformat()

        out = []
        for d in planned:
            sym, sc = d["symbol"], scored[d["symbol"]]
            g = sc["graham"]
            conf, conf_method = confidence(sc["composite"], calib)
            facts = {
                "price": round(sc["price"], 2),
                "iv_low": round(g.iv_low, 2) if g.iv_low else None,
                "iv_mid": round(g.iv_mid, 2) if g.iv_mid else None,
                "iv_high": round(g.iv_high, 2) if g.iv_high else None,
                "mos_band_low": round(g.mos_band_low, 4) if g.mos_band_low is not None else None,
                "g_pct": round(g.g_pct, 2),
                "aaa_yield": g.aaa_yield,
                "pe_3y": round(g.pe_3y, 2) if g.pe_3y else None,
                "current_ratio": round(g.current_ratio, 2) if g.current_ratio else None,
                "criteria_passed": g.criteria_passed,
                "criteria_total": len(graham.CRITERIA),
                "composite_score": round(sc["composite"], 2),
                "threshold": d["threshold"],
                "text_score": round(sc["text"], 2),
                "text_items": len(sc["texts"]),
                "target_weight": d["weight"],
                "confidence": conf,
                "score_max": 4,
            }
            thesis, check = self._thesis(d["action"], sym, facts)
            decision_id = f"{as_of}-{sym}-{d['action']}"
            record = {
                "decision_id": decision_id,
                "as_of": f"{as_of}T21:00:00Z",
                "snapshot_hash": digest,
                "code_version": self.s.code_version,
                "framework": FRAMEWORK_VERSION,
                "model": {"scorer": self.s.scoring_model, "writer": self.s.thesis_model},
                "ticker": sym,
                "sector": by_symbol.get(sym, {}).get("sector"),
                "action": d["action"],
                "target_weight": d["weight"],
                "confidence": conf,
                "confidence_method": conf_method,
                "computed": {**facts, "criteria": g.criteria, "value_score": round(sc["value"], 3), "quality_score": round(sc["quality"], 3)},
                "risk": d["risk"],
                "reasons": d["reasons"],
                "thesis": thesis,
                "checks": {"number_grounding": check, "schema": "pass"},
                "resolve_on": resolve_on,
            }
            self.db.upsert(
                "decisions",
                [
                    {
                        "decision_id": decision_id,
                        "as_of": record["as_of"],
                        "date": as_of,
                        "symbol": sym,
                        "action": d["action"],
                        "target_weight": d["weight"],
                        "confidence": conf,
                        "record": json.dumps(record, default=str),
                        "snapshot_hash": digest,
                        "resolve_on": resolve_on,
                        "thesis_ok": 1 if check == "pass" else 0,
                    }
                ],
                ["decision_id"],
                update=False,
            )
            if d["action"] in ("ACCUMULATE", "EXIT"):
                self.db.upsert(
                    "orders",
                    [
                        {
                            "order_id": decision_id,
                            "decision_id": decision_id,
                            "symbol": sym,
                            "side": "BUY" if d["action"] == "ACCUMULATE" else "SELL",
                            "target_weight": d["weight"],
                            "created_date": as_of,
                            "status": "pending",
                            "fill_date": None,
                            "fill_price": None,
                            "amount": None,
                        }
                    ],
                    ["order_id"],
                    update=False,
                )
            out.append(f"{d['action']} {sym}")
        self.notes.append("decisions: " + (", ".join(out) or "none"))
        return out

    def _thesis(self, action: str, symbol: str, facts: dict[str, Any]) -> tuple[list[str], str]:
        if action == "SKIP_RISK":
            return [], "n/a"
        for _ in range(2):
            try:
                bullets = self.llm.write_thesis(action, symbol, facts)
            except Exception as e:
                log.warning("thesis for %s failed: %s", symbol, e)
                break
            ok, bad = grounding_check(bullets, facts)
            if ok and bullets:
                return bullets, "pass"
            log.warning("thesis for %s failed grounding: %s", symbol, bad)
        return template_thesis(action, symbol, facts), "failed_llm_used_template"

    # --------------------------------------------------------------- health
    def health_checks(self, as_of: str) -> None:
        spend, cap = self.llm.month_spend(), self.s.llm_monthly_cap_usd
        if spend >= 0.8 * cap:
            self.alerts.alert("LLM spend above 80% of monthly cap", f"${spend:.2f} of ${cap:.2f} this month.")
        size = self.db.size_mb()
        if size is not None and size > self.s.db_size_alert_mb:
            self.alerts.alert("Database size warning", f"Database is {size:.0f} MB (free tier limit 500 MB).")
        navs = [r["ret"] for r in self.db.query("SELECT ret FROM nav ORDER BY date DESC LIMIT 61")]
        if len(navs) >= 30:
            sd = stats.stdev(navs[1:])
            if sd and abs(navs[0]) > 3 * sd:
                self.alerts.alert("Portfolio volatility spike", f"{as_of}: daily return {navs[0]:+.2%} vs 60-day sd {sd:.2%}.")
        g = evaluation.grounding_failure_rate(self.db)
        if g["decisions"] >= 20 and g["failure_rate"] > 0.05:
            self.alerts.alert("Thesis grounding failures above 5%", json.dumps(g))
        c = evaluation.consistency_rate(self.db)
        if c["pairs"] >= 20 and c["agreement"] < 0.8:
            self.alerts.alert("Text scoring consistency below 80%", json.dumps(c))

    # -------------------------------------------------------------- monthly
    def monthly_report(self, as_of: str) -> str:
        panel = evaluation.ReturnPanel(self.db, _minus(as_of, 2000))
        evaluation.update_ic(self.db, panel)
        evaluation.tag_closed_positions(self.db, panel, SECTOR_ETFS)
        m = metrics(self.db)
        ic = evaluation.ic_summary(self.db)
        brier = evaluation.decision_brier(self.db, panel)
        gate = evaluation.learning_gate(self.db)
        cons = evaluation.consistency_rate(self.db)
        ground = evaluation.grounding_failure_rate(self.db)
        tags: dict[str, int] = defaultdict(int)
        for r in self.db.query("SELECT tags FROM closed_positions WHERE tags IS NOT NULL"):
            for t in json.loads(r["tags"]):
                tags[t] += 1
        lines = [
            f"# AMIA monthly report: {as_of[:7]}",
            "",
            "## Portfolio",
            *(f"- {k}: {v:.4f}" if isinstance(v, float) else f"- {k}: {v}" for k, v in m.items()),
            "",
            "## Ranking quality (rank IC vs forward excess return)",
            *(f"- {h}d: mean IC {v['mean_ic']}, overlap-adjusted t {v['t_stat_overlap_adj']}, days {v['days']}" for h, v in ic.items()),
            "",
            "## Calibration (decisions)",
            f"- {json.dumps(brier)}",
            "",
            "## Logic checks",
            f"- Thesis grounding: {json.dumps(ground)}",
            f"- Scoring consistency: {json.dumps(cons)}",
            "",
            "## Attribution tags (closed positions)",
            *(f"- {k}: {v}" for k, v in sorted(tags.items())),
            "",
            "## Learning gate",
            f"- Unlocked: {gate['unlocked']} ({gate['resolved_days']}/{gate['required_days']} resolved days)",
            f"- {gate['note']}",
        ]
        report = "\n".join(lines)
        self.db.set_kv(f"report:{as_of[:7]}", report)
        return report

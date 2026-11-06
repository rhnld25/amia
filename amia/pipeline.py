"""Daily orchestration (Component 7):
news and filings -> fast-tier scoring -> news-driven watchlist -> fills -> framework math ->
reasoning model decides -> code enforces risk limits and number grounding -> evaluation.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import casestudies, evaluation, learning, retention, risk, stats
from .alerts import Alerter
from .config import (
    BENCHMARKS,
    DECISION,
    DISCOVERY,
    EVALUATION,
    FRAMEWORK,
    FRAMEWORK_VERSION,
    MACRO_SERIES,
    PORTFOLIO,
    RISK,
    SECTOR_ETFS,
    Settings,
)
from .data import Providers, get_providers
from .data.sectors import sector_for_sic
from .db import DB
from .decision import grounding_check, validate_decision
from .discovery import attention_table, build_watchlist, keep_news
from .frameworks import buffett, graham, marks
from .llm import DECISION_PROMPT_VERSION, LLM, PRINCIPLES_VERSION, RUBRIC_VERSION, BudgetExceeded
from .portfolio import metrics, positions, run_day
from .snapshots import save_snapshot

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _minus(d: str, days: int) -> str:
    return (date.fromisoformat(d) - timedelta(days=days)).isoformat()


def _pipe(symbols: list[str]) -> str:
    return "|" + "|".join(symbols) + "|"


def _key(ticker: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", ticker.upper())


def _r(v: Any, k: int = 4) -> Any:
    if isinstance(v, float):
        return round(v, k)
    if isinstance(v, dict):
        return {a: _r(b, k) for a, b in v.items()}
    if isinstance(v, list):
        return [_r(b, k) for b in v]
    return v


def _in(prefix: str, values: list[str]) -> tuple[str, dict[str, str]]:
    params = {f"{prefix}{i}": v for i, v in enumerate(values)}
    return "(" + ",".join(f":{k}" for k in params) + ")", params


class Pipeline:
    def __init__(self, settings: Settings, db: DB | None = None, providers: Providers | None = None):
        self.s = settings
        self.db = db or DB(settings.database_url)
        self.db.init_schema()
        self.p = providers or get_providers(settings)
        self.llm = LLM(settings, self.db)
        self.alerts = Alerter(settings)
        self.notes: list[str] = []

    # ------------------------------------------------------- reference data
    def ticker_map(self, as_of: str) -> dict[str, dict[str, Any]]:
        if self.db.get_kv("tickers_month") != as_of[:7]:
            try:
                tmap = self.p.ticker_map()
                self.db.upsert("tickers", [{"key": k, **v} for k, v in tmap.items()], ["key"])
                self.db.set_kv("tickers_month", as_of[:7])
            except Exception as e:
                self.alerts.alert("SEC ticker map refresh failed", f"{e}. Using the stored map.")
        return {r["key"]: r for r in self.db.query("SELECT * FROM tickers")}

    def ensure_profiles(self, symbols: list[str], tmap: dict[str, dict[str, Any]], as_of: str) -> dict[str, dict[str, Any]]:
        known = {r["symbol"]: r for r in self.db.query("SELECT * FROM symbols")}
        new = []
        for s in symbols:
            if s in known:
                continue
            t = tmap.get(_key(s))
            row = {"symbol": s, "cik": None, "name": None, "sector": "Unknown", "sic": None, "first_seen": as_of, "filings_checked": None, "no_price_since": None}
            if t:
                row.update(cik=t["cik"], name=t["title"])
                try:
                    prof = self.p.company_profile(t["cik"])
                    row.update(name=prof.get("name") or t["title"], sic=prof.get("sic"), sector=sector_for_sic(prof.get("sic")))
                except Exception as e:
                    log.warning("profile %s failed: %s", s, e)
            new.append(row)
            known[s] = row
        self.db.upsert("symbols", new, ["symbol"])
        return {s: known[s] for s in symbols if s in known}

    # ----------------------------------------------------------------- data
    def update_prices(self, symbols: list[str], as_of: str) -> None:
        have = {r["symbol"]: r["last"] for r in self.db.query("SELECT symbol, MAX(date) AS last FROM prices GROUP BY symbol")}
        groups: dict[str, list[str]] = defaultdict(list)
        for s in set(symbols):
            groups[have.get(s) or _minus(as_of, 420)].append(s)
        for start, syms in groups.items():
            if start >= as_of:
                continue
            bars = self.p.daily_bars(sorted(syms), start, as_of)
            rows = [{"symbol": s, **b} for s, bs in bars.items() for b in bs]
            self.db.upsert("prices", rows, ["symbol", "date"])

    def update_macro(self, as_of: str) -> None:
        for series in MACRO_SERIES:
            last = self.db.scalar("SELECT MAX(date) FROM macro WHERE series = :s", {"s": series})
            try:
                obs = self.p.macro(series, last or _minus(as_of, FRAMEWORK["hy_lookback_days"]), as_of)
            except Exception as e:
                self.alerts.alert(f"FRED fetch failed: {series}", str(e))
                continue
            self.db.upsert("macro", [{"series": series, "date": d, "value": v} for d, v in obs], ["series", "date"])

    def macro_until(self, as_of: str) -> dict[str, list[tuple[str, float]]]:
        out: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for r in self.db.query(
            "SELECT series, date, value FROM macro WHERE date <= :d AND date >= :s ORDER BY date",
            {"d": as_of, "s": _minus(as_of, FRAMEWORK["hy_lookback_days"])},
        ):
            out[r["series"]].append((r["date"], r["value"]))
        return out

    def refresh_facts(self, ciks: list[str], limit: int) -> int:
        fresh = {r["cik"] for r in self.db.query("SELECT cik FROM facts_status WHERE fetched_at IS NOT NULL")}
        todo = [c for c in dict.fromkeys(ciks) if c and c not in fresh][:limit]
        for cik in todo:
            try:
                rows = self.p.company_facts(cik)
                self.db.upsert("facts", rows, ["cik", "concept", "period_end", "accn"], update=False)
                last = max((r["filed"] for r in rows), default=None)
                self.db.upsert("facts_status", [{"cik": cik, "fetched_at": _now(), "last_filed": last, "error": None}], ["cik"])
            except Exception as e:
                log.warning("companyfacts %s failed: %s", cik, e)
                self.db.upsert("facts_status", [{"cik": cik, "fetched_at": None, "last_filed": None, "error": str(e)[:300]}], ["cik"])
        return len(todo)

    # ----------------------------------------------------------------- text
    def scan_filings(self, entries: list[dict[str, Any]], as_of: str) -> None:
        """New 10-K/10-Q/8-K filings for watched companies. A company's first scan looks back for its
        latest periodic report (for management commentary) and any 8-Ks from the last two weeks."""
        known = {r["accn"] for r in self.db.query("SELECT accn FROM filings WHERE filed >= :s", {"s": _minus(as_of, 120)})}
        new_rows, stale, errors = [], set(), 0
        for e in entries:
            first = not e.get("filings_checked")
            since = _minus(as_of, 100) if first else e["filings_checked"]
            try:
                filings = [f for f in self.p.recent_filings(e["cik"], since) if f["filed"] <= as_of and f["accn"] not in known]
            except Exception as ex:
                errors += 1
                log.warning("submissions %s failed: %s", e["cik"], ex)
                continue
            if first:
                periodic = sorted((f for f in filings if f["form"] in ("10-K", "10-Q")), key=lambda f: f["filed"])[-1:]
                recent_8k = [f for f in filings if f["form"] == "8-K" and f["filed"] >= _minus(as_of, 14)]
                filings = periodic + recent_8k
            for f in filings:
                known.add(f["accn"])
                new_rows.append({**f, "cik": e["cik"], "symbol": e["symbol"], "url": "", "status": "new", "created_at": _now()})
                if f["form"] in ("10-K", "10-Q") and not first:
                    stale.add(e["cik"])
            self.db.execute("UPDATE symbols SET filings_checked = :d WHERE symbol = :s", {"d": _minus(as_of, 3), "s": e["symbol"]})
        self.db.upsert("filings", new_rows, ["accn"], update=False)
        for cik in stale:
            self.db.execute("UPDATE facts_status SET fetched_at = NULL WHERE cik = :c", {"c": cik})
        self.notes.append(f"filings: {len(new_rows)} new, {len(stale)} companies with new financials")
        if entries and errors / len(entries) > 0.2:
            self.alerts.alert("EDGAR submissions errors", f"{errors} of {len(entries)} companies failed on {as_of}.")

    def ingest_filings(self, limit: int = 80) -> None:
        items = []
        for f in self.db.query("SELECT * FROM filings WHERE status = 'new' ORDER BY filed DESC LIMIT :n", {"n": limit}):
            try:
                body = self.p.filing_text(f["cik"], f["accn"], f["primary_doc"], f["form"])
                items.append(self._text_item(f"f-{f['accn']}", f["form"], [f["symbol"]], f["accepted"] or f["filed"], body))
                self.db.execute("UPDATE filings SET status = 'fetched' WHERE accn = :a", {"a": f["accn"]})
            except Exception as e:
                log.warning("filing text %s failed: %s", f["accn"], e)
                self.db.execute("UPDATE filings SET status = 'text_failed' WHERE accn = :a", {"a": f["accn"]})
        self.db.upsert("text_items", items, ["item_id"], update=False)
        self.notes.append(f"filing texts queued: {len(items)}")

    def _text_item(self, item_id: str, item_type: str, symbols: list[str], published: str, body: str) -> dict[str, Any]:
        return {
            "item_id": item_id,
            "item_type": item_type,
            "symbols": _pipe(symbols),
            "published_at": published,
            "body": gzip.compress(body.encode()),
            "status": "pending",
            "batch_id": None,
            "recheck": 0,
            "created_at": _now(),
        }

    def ingest_news(self, as_of: str, live_clock: bool, always: set[str], since: str | None = None, cap: int | None = None) -> None:
        """All market news since the last run, filtered for high-impact keywords and away from roundups."""
        since = since or self.db.get_kv("news_since") or f"{_minus(as_of, 2)}T00:00:00Z"
        end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if live_clock else f"{as_of}T23:59:59Z"
        try:
            news = self.p.news(since, end)
        except Exception as e:
            self.alerts.alert("News fetch failed", f"{e}. Discovery uses already-scored items until the feed returns.")
            return
        latest, kept = since, []
        for n in news:
            latest = max(latest, n["created_at"])
            syms = keep_news(n, always)
            if syms:
                body = f"{n['headline']}\n\n{n['summary']}".strip()
                kept.append((any(s in always for s in syms), self._text_item(f"n-{n['id']}", "news", syms, n["created_at"], body)))
        cap = cap or DISCOVERY["max_news_per_run"]
        kept.sort(key=lambda x: (not x[0], x[1]["published_at"]), reverse=False)
        items = [it for _, it in kept[:cap]]
        self.db.set_kv("news_since", latest)
        self.db.upsert("text_items", items, ["item_id"], update=False)
        self.notes.append(f"news: {len(news)} fetched, {len(kept)} passed the noise filter, {len(items)} queued")

    def score_text(self, wait_minutes: float | None = None) -> None:
        pending = self.db.query("SELECT * FROM text_items WHERE status IN ('pending', 'retry') ORDER BY published_at LIMIT 8000")
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
            wait = (wait_minutes if wait_minutes is not None else self.s.batch_wait_minutes) if b["batch_id"] == new_batch else 0
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
        if not results:
            return
        ids = list(dict.fromkeys(cid.removesuffix("--r") for cid in results))
        clause, params = _in("i", ids)
        items = {r["item_id"]: r for r in self.db.query(f"SELECT item_id, item_type, symbols, published_at, status FROM text_items WHERE item_id IN {clause}", params)}
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
                    **{k: res[k] for k in ("novelty", "sentiment", "relevance", "rationale", "citation")},
                    "model": self.s.scoring_model if not self.llm.fake else "fake",
                    "rubric_version": RUBRIC_VERSION,
                    "scored_at": _now(),
                }
            )
            if not is_recheck:
                self.db.execute("UPDATE text_items SET status = 'scored' WHERE item_id = :i", {"i": item_id})
        self.db.upsert("text_scores", rows, ["item_id", "score_pass"])

    # ------------------------------------------------------------ discovery
    def rank_watchlist(self, as_of: str, held: set[str], tmap: dict[str, dict[str, Any]], prices_through: str) -> list[dict[str, Any]]:
        """The watchlist from news scored so far. Prices are only fetched through `prices_through`, so an
        intraday refresh never stores a half-finished daily bar."""
        items = self.db.query(
            "SELECT symbols, published_at, novelty, sentiment, relevance FROM text_scores "
            "WHERE score_pass = 'primary' AND published_at >= :a AND published_at <= :b",
            {"a": _minus(as_of, DISCOVERY["lookback_days"]), "b": f"{as_of}T23:59:59Z"},
        )
        table = attention_table(items, as_of)
        # look at twice the list size so names without prices can be replaced
        candidates = sorted((s for s, r in table.items() if r["attention"] >= DISCOVERY["min_attention"]), key=lambda s: -table[s]["attention"])
        candidates = candidates[: 2 * DISCOVERY["watchlist_size"]]
        profiles = self.ensure_profiles(candidates + sorted(held), tmap, as_of)
        self.update_prices(candidates, prices_through)
        priced = {r["symbol"] for r in self.db.query("SELECT DISTINCT symbol FROM prices WHERE date >= :d", {"d": _minus(as_of, 7)})}
        unpriced = [s for s in candidates if s not in priced]
        if unpriced:
            self.notes.append(f"no price data, left off the watchlist: {', '.join(unpriced[:15])}")
        company = {s: p["cik"] for s, p in profiles.items() if p.get("cik")}
        return build_watchlist(table, held, eligible=priced, company=company, dollar_volume=self._dollar_volume(candidates, as_of))

    def _dollar_volume(self, symbols: list[str], as_of: str) -> dict[str, float]:
        """Average daily dollar volume over the last ~20 trading days (volume is stored from Nov 2026 on)."""
        if not symbols:
            return {}
        clause, params = _in("s", sorted(set(symbols)))
        return {
            r["symbol"]: float(r["dv"])
            for r in self.db.query(
                f"SELECT symbol, AVG(close * volume) AS dv FROM prices WHERE symbol IN {clause} AND date > :a AND date <= :d "
                "AND volume IS NOT NULL GROUP BY symbol",
                {**params, "a": _minus(as_of, 30), "d": as_of},
            )
            if r["dv"] is not None
        }

    def discover(self, as_of: str, held: set[str], tmap: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        wl = self.rank_watchlist(as_of, held, tmap, as_of)
        self.db.execute("DELETE FROM watchlist WHERE date = :d", {"d": as_of})
        self.db.upsert(
            "watchlist",
            [{k: w.get(k) for k in ("symbol", "rank", "attention", "weight", "items", "avg_sentiment", "sentiment_shift", "max_relevance", "held")} | {"date": as_of} for w in wl],
            ["date", "symbol"],
        )
        prev = {r["symbol"] for r in self.db.query("SELECT symbol FROM watchlist WHERE date = (SELECT MAX(date) FROM watchlist WHERE date < :d)", {"d": as_of})}
        entered = [w["symbol"] for w in wl if w["symbol"] not in prev]
        self.notes.append(f"watchlist: {len(wl)} names, {len(entered)} new ({', '.join(entered[:10])})")
        return wl

    # ------------------------------------------------------------- intraday
    def intraday(self, as_of: str) -> dict[str, Any]:
        """News and watchlist refresh during the day. No decisions, no trades and no change to the official
        watchlist: the evening run still decides on the list as of the close. The result is kept in kv for
        the dashboard."""
        run_id = uuid.uuid4().hex[:12]
        self.notes = []
        self.db.upsert("runs", [{"run_id": run_id, "kind": "intraday", "as_of": as_of, "started": _now(), "finished": None, "status": "running", "notes": None}], ["run_id"])
        status = "ok"
        try:
            held = set(positions(self.db))
            last = self.db.scalar("SELECT MAX(date) FROM watchlist WHERE date <= :d", {"d": as_of})
            official = [r["symbol"] for r in self.db.query("SELECT symbol FROM watchlist WHERE date = :d", {"d": last})] if last else []
            tmap = self.ticker_map(as_of)
            self.ingest_news(as_of, True, held | set(official))
            self.score_text()
            wl = self.rank_watchlist(as_of, held, tmap, _minus(as_of, 1))
            entered = [w["symbol"] for w in wl if w["rank"] and w["symbol"] not in official]
            self.db.set_kv("watchlist:live", {"updated": _now(), "as_of": as_of, "since": last, "rows": wl})
            self.notes.append(f"live watchlist: {len(wl)} names, {len(entered)} new since the {last or 'first'} close ({', '.join(entered[:10])})")
            return {"status": "ok", "watchlist": len(wl), "entered": entered}
        except Exception as e:
            status = "error"
            self.alerts.alert("Intraday refresh failed", f"{as_of}: {type(e).__name__}: {e}")
            raise
        finally:
            self.db.execute(
                "UPDATE runs SET finished = :f, status = :s, notes = :n WHERE run_id = :r",
                {"f": _now(), "s": status, "n": json.dumps(self.notes + [f"alert: {a}" for a in self.alerts.sent]), "r": run_id},
            )

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
        held = set(positions(self.db))
        prev_wl = [r["symbol"] for r in self.db.query("SELECT symbol FROM watchlist WHERE date = (SELECT MAX(date) FROM watchlist WHERE date < :d)", {"d": as_of})]
        self.update_prices(BENCHMARKS + sorted(held) + prev_wl, as_of)
        if not self.db.scalar("SELECT 1 FROM prices WHERE symbol = 'SPY' AND date = :d", {"d": as_of}):
            self.notes.append("market closed or data not yet available; no decisions")
            return {"status": "market_closed"}
        self.update_macro(as_of)
        tmap = self.ticker_map(as_of)

        # Component 1: news and filings for what we already watch, scored by the fast tier
        always = held | set(prev_wl)
        self.ingest_news(as_of, live_clock, always)
        watched = self.ensure_profiles(sorted(always), tmap, as_of)
        self.scan_filings([w for w in watched.values() if w["cik"]], as_of)
        self.ingest_filings()
        self.score_text()

        wl = self.discover(as_of, held, tmap)
        profiles = self.ensure_profiles([w["symbol"] for w in wl], tmap, as_of)
        new_entries = [p for p in profiles.values() if p["cik"] and not p["filings_checked"]]
        self.scan_filings(new_entries, as_of)  # their filings are scored in the next run's batch
        self.refresh_facts([p["cik"] for p in profiles.values() if p["cik"]], facts_limit)

        today = {r["symbol"]: r for r in self.db.query("SELECT * FROM prices WHERE date = :d", {"d": as_of})}
        prev_date = self.db.scalar("SELECT MAX(date) FROM prices WHERE symbol = 'SPY' AND date < :d", {"d": as_of})
        prev_close = {r["symbol"]: r["close"] for r in self.db.query("SELECT symbol, close FROM prices WHERE date = :d", {"d": prev_date})} if prev_date else {}

        # quality gates
        symbols = [w["symbol"] for w in wl]
        missing = [s for s in symbols if s not in today]
        halt = bool(symbols) and len(missing) / len(symbols) > 0.25
        if halt:
            self.alerts.alert("Data gate: missing prices", f"{len(missing)} of {len(symbols)} watchlist prices missing on {as_of}; decisions halted.")
        elif missing:
            self.notes.append(f"no bar today, not reviewed: {', '.join(missing)}")
        jumps = [s for s in symbols if s in today and today[s]["ret"] is not None and abs(today[s]["ret"]) >= 0.5]
        if jumps:
            self.alerts.alert("Data gate: one-day moves of 50% or more", f"{as_of}: {', '.join(jumps)} excluded from decisions today.")
        macro = self.macro_until(as_of)
        aaa_series = macro.get("DAAA") or macro.get("AAA") or []
        if not aaa_series or (date.fromisoformat(as_of) - date.fromisoformat(aaa_series[-1][0])).days > 7:
            self.alerts.alert("Data gate: AAA yield stale", f"Latest AAA observation: {aaa_series[-1] if aaa_series else 'none'}")

        ctx, market = self.compute_features(wl, profiles, as_of, today, macro)
        size_of = {s: c["size"] for s, c in ctx.items()}
        for s, p in positions(self.db).items():
            size_of.setdefault(s, "small")
        rf = (macro.get("DTB3") or [(None, 0.0)])[-1][1]
        sector_of = {r["symbol"]: r["sector"] for r in self.db.query("SELECT symbol, sector FROM symbols")}
        port = run_day(self.db, as_of, today, prev_close, size_of, sector_of, today.get("SPY", {}).get("ret"), rf)
        self.notes.extend(port.get("events", []))

        if halt:
            decisions = []
        elif self.db.scalar("SELECT 1 FROM decisions WHERE date = :d LIMIT 1", {"d": as_of}):
            decisions = []
            self.notes.append("decisions already made for this date; not re-run")
        else:
            decisions = self.decide(as_of, wl, ctx, market, macro, set(jumps))
        self._fill_llm_features(as_of)

        panel = evaluation.ReturnPanel(self.db, _minus(as_of, 420))
        evaluation.update_ic(self.db, panel)
        evaluation.tag_closed_positions(self.db, panel, SECTOR_ETFS)
        self.health_checks(as_of)
        pruned = retention.prune(self.db, as_of)
        if any(pruned.values()):
            self.notes.append("pruned: " + ", ".join(f"{n} {k}" for k, n in pruned.items() if n))
        return {"status": "ok", "nav": port.get("nav"), "decisions": decisions, "watchlist": len(wl)}

    # -------------------------------------------------------------- features
    def _returns(self, symbols: list[str], as_of: str) -> dict[str, dict[str, float]]:
        if not symbols:
            return {}
        clause, params = _in("s", sorted(set(symbols)))
        out: dict[str, dict[str, float]] = defaultdict(dict)
        for r in self.db.query(
            f"SELECT symbol, date, ret FROM prices WHERE symbol IN {clause} AND date > :a AND date <= :d",
            {**params, "a": _minus(as_of, 400), "d": as_of},
        ):
            if r["ret"] is not None:
                out[r["symbol"]][r["date"]] = r["ret"]
        return out

    def compute_features(self, wl, profiles, as_of: str, today, macro) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """Component 2 math for every watchlist name: Graham, Buffett, Marks, momentum, volatility, beta."""
        aaa_series = macro.get("DAAA") or macro.get("AAA") or []
        aaa = aaa_series[-1][1] if aaa_series else None
        t10 = (macro.get("DGS10") or [(None, None)])[-1][1]
        symbols = [w["symbol"] for w in wl]
        rets = self._returns(symbols + BENCHMARKS, as_of)
        spy = rets.get("SPY", {})
        series = lambda s: [rets.get(s, {})[d] for d in sorted(rets.get(s, {}))]  # noqa: E731
        market = marks.market_cycle(macro, series("SPY"))
        regime = risk.regime_flags(macro)
        market["regime_flags"] = regime
        spy_3m = marks.price_stats(series("SPY"))["ret_3m"]
        sector_rel = {}
        for sector, etf in SECTOR_ETFS.items():
            r3 = marks.price_stats(series(etf))["ret_3m"]
            sector_rel[sector] = (etf, r3 - spy_3m if r3 is not None and spy_3m is not None else None)
        market["sector_etf_ret_3m_vs_spy"] = {etf: _r(v) for etf, v in sector_rel.values()}
        self.db.upsert(
            "market_state",
            [{"date": as_of, "regime_flags": len(regime), "temperature_score": market["temperature_score"], "record": json.dumps(_r(market), default=str)}],
            ["date"],
        )

        ranked_att = sorted(w["attention"] for w in wl if w["rank"])
        top_q = ranked_att[int(len(ranked_att) * 0.75)] if ranked_att else float("inf")
        ciks = [profiles[s]["cik"] for s in symbols if profiles.get(s, {}).get("cik")]
        facts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if ciks:
            clause, params = _in("c", sorted(set(ciks)))
            for r in self.db.query(
                f"SELECT cik, concept, period_end, value, filed FROM facts WHERE cik IN {clause} AND filed <= :d", {**params, "d": as_of}
            ):
                facts[r["cik"]].append(r)

        ctx: dict[str, dict[str, Any]] = {}
        rows = []
        for w in wl:
            s = w["symbol"]
            bar = today.get(s)
            if not bar:
                continue
            prof = profiles.get(s) or {"cik": None, "name": None, "sector": "Unknown"}
            pit = graham.point_in_time(facts.get(prof["cik"], []), as_of) if prof["cik"] else {}
            g = graham.evaluate(pit, bar["close"], aaa) if pit and aaa else None
            b = buffett.evaluate(pit, bar["close"], t10) if pit else None
            ps = marks.price_stats(series(s))
            pend = marks.stock_pendulum(ps, w["avg_sentiment"], w["attention"] >= top_q and w["rank"] is not None)
            vol = risk.annual_vol(rets.get(s, {}))
            beta = risk.beta(rets.get(s, {}), spy)
            mcap = b.market_cap if b else None
            etf, rel = sector_rel.get(prof["sector"], (None, None))
            ctx[s] = {
                "watch": w,
                "profile": prof,
                "pit": pit,
                "graham": g,
                "buffett": b,
                "price_stats": ps,
                "pendulum": pend,
                "vol": vol,
                "beta": beta,
                "price": bar["close"],
                "sector_etf": etf,
                "sector_rel_3m": rel,
                "size": "large" if mcap and mcap >= PORTFOLIO["large_cap_usd"] else "small",
            }
            rows.append(
                {
                    "date": as_of,
                    "symbol": s,
                    "sector": prof["sector"],
                    "price": bar["close"],
                    "attention": w["attention"],
                    "avg_sentiment": w["avg_sentiment"],
                    "graham_mos": g.mos_band_low if g else None,
                    "graham_passed": g.criteria_passed if g else None,
                    "iv_mid": g.iv_mid if g else None,
                    "buffett_passed": b.criteria_passed if b else None,
                    "roe_avg": b.roe_avg if b else None,
                    "marks_pendulum": pend["score"],
                    "mom_12_1": ps["mom_12_1"],
                    "vol": vol,
                    "beta": beta,
                    "llm_conf": None,
                    "llm_action": None,
                }
            )
        self.db.upsert("features", rows, ["date", "symbol"])
        self.notes.append(f"framework math for {len(rows)} names ({sum(1 for c in ctx.values() if c['graham'])} with SEC fundamentals)")
        return ctx, market

    def _fill_llm_features(self, as_of: str) -> None:
        """Carry each name's latest review (up to the re-review interval) into today's features, for the IC."""
        for f in self.db.query("SELECT symbol FROM features WHERE date = :d", {"d": as_of}):
            r = self.db.query(
                "SELECT action, confidence FROM decisions WHERE symbol = :s AND date <= :d AND date >= :a ORDER BY date DESC LIMIT 1",
                {"s": f["symbol"], "d": as_of, "a": _minus(as_of, DECISION["review_stale_days"] + 2)},
            )
            if r:
                self.db.execute(
                    "UPDATE features SET llm_conf = :c, llm_action = :a WHERE date = :d AND symbol = :s",
                    {"c": r[0]["confidence"], "a": r[0]["action"], "d": as_of, "s": f["symbol"]},
                )

    # ------------------------------------------------------------ decisions
    def _news_for(self, symbol: str, as_of: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT s.item_id, s.item_type, s.published_at, s.novelty, s.sentiment, s.relevance, s.rationale, s.citation, t.body "
            "FROM text_scores s JOIN text_items t ON t.item_id = s.item_id "
            "WHERE s.score_pass = 'primary' AND s.symbols LIKE :sym AND s.published_at >= :a AND s.published_at <= :b "
            "ORDER BY s.relevance DESC, s.published_at DESC LIMIT :n",
            {"sym": f"%|{symbol}|%", "a": _minus(as_of, FRAMEWORK["text_lookback_days"]), "b": f"{as_of}T23:59:59Z", "n": DECISION["max_news_in_dossier"]},
        )
        out = []
        for r in rows:
            body = gzip.decompress(bytes(r["body"])).decode()
            head = body.split("\n", 1)[0][:240] if r["item_type"] == "news" else body[:400]
            out.append(
                {
                    "published": r["published_at"][:10],
                    "type": r["item_type"],
                    "text": head,
                    "novelty": r["novelty"],
                    "sentiment": r["sentiment"],
                    "relevance": r["relevance"],
                    "rationale": r["rationale"],
                }
            )
        return out

    def _dossier(self, s: str, c: dict[str, Any], as_of: str, state: risk.PortfolioState, regime: list[str], market: dict[str, Any], held: dict[str, Any] | None, buys_left: int) -> dict[str, Any]:
        sector = c["profile"]["sector"]
        req, reasons = risk.required_confidence(sector, state, regime)
        g, b, w, ps = c["graham"], c["buffett"], c["watch"], c["price_stats"]
        if g:
            gd = g.to_dict()
            graham_view = {k: gd[k] for k in ("eps", "eps_3y_avg", "g_pct", "aaa_yield", "iv_low", "iv_mid", "iv_high", "mos_band_low", "current_ratio", "pe_3y", "pb", "revenue", "years_positive_eps", "years_dividends", "criteria_passed", "criteria_total")}
            graham_view["criteria"] = gd["criteria"]
        else:
            graham_view = {"available": False, "reason": "no SEC XBRL fundamentals for this ticker" if not c["profile"]["cik"] else "fundamentals not loaded yet"}
        buffett_view = b.to_dict() if b else {"available": False}
        dossier = {
            "ticker": s,
            "name": c["profile"]["name"],
            "sector": sector,
            "held": None,
            "price": {
                "close": c["price"],
                **ps,
                "vol_ann": c["vol"],
                "beta_spy": c["beta"],
                "sector_etf": c["sector_etf"],
                "sector_etf_ret_3m_vs_spy": c["sector_rel_3m"],
            },
            "attention": {
                "watchlist_rank": w["rank"],
                "attention": w["attention"],
                "watchlist_weight": w["weight"],
                "items_7d": w["items"],
                "avg_sentiment": w["avg_sentiment"],
                "sentiment_shift": w["sentiment_shift"],
                "max_relevance": w["max_relevance"],
            },
            "graham": graham_view,
            "buffett": buffett_view,
            "marks": {"stock": c["pendulum"], "market": {k: v for k, v in market.items() if k != "sector_etf_ret_3m_vs_spy"}},
            "news": self._news_for(s, as_of),
            "portfolio": {
                "nav": state.nav,
                "cash_weight": 1 - sum(state.weights.values()),
                "positions": len(state.weights),
                "sector_weight": state.sector_weight(sector),
                "portfolio_beta": state.beta(),
                "new_positions_left_today": buys_left,
            },
            "risk_overlay": {"required_confidence": req, "reasons": reasons, "max_position_weight": DECISION["max_position_weight"]},
            "lessons": learning.lessons_for(self.db, sector),
        }
        if held:
            dossier["held"] = {
                "weight": held["value"] / state.nav,
                "entry_price": held["entry_price"],
                "return_since_entry": held["value"] / held["cost"] - 1,
                "days_held": (date.fromisoformat(as_of) - date.fromisoformat(held["opened_date"])).days,
            }
        return _r(dossier)

    def _needs_review(self, s: str, c: dict[str, Any], as_of: str) -> bool:
        last = self.db.query("SELECT date, record FROM decisions WHERE symbol = :s ORDER BY date DESC LIMIT 1", {"s": s})
        if not last:
            return True
        if (date.fromisoformat(as_of) - date.fromisoformat(last[0]["date"])).days >= DECISION["review_stale_days"]:
            return True
        prev_price = (json.loads(last[0]["record"]).get("dossier_price") or {}).get("close")
        if prev_price and abs(c["price"] / prev_price - 1) >= DECISION["review_price_move"]:
            return True
        newer = self.db.scalar(
            "SELECT COUNT(*) FROM text_scores WHERE score_pass = 'primary' AND symbols LIKE :sym AND scored_at > :t",
            {"sym": f"%|{s}|%", "t": f"{last[0]['date']}T23:59:59"},
        )
        return bool(newer)

    def _ask(self, dossier: dict[str, Any], held: bool) -> dict[str, Any]:
        """One reasoning call, with one retry if the answer is malformed or uses numbers not in the dossier."""
        feedback, answer = None, None
        for attempt in range(2):
            raw = self.llm.decide(dossier, feedback)
            answer, problems = validate_decision(raw, dossier["ticker"], held)
            ok, bad = grounding_check(answer["thesis"], [dossier, answer["confidence"], answer["target_allocation"]])
            answer["checks"] = {"schema": "pass" if not problems else problems, "number_grounding": "pass" if ok else {"failed": bad}, "attempts": attempt + 1}
            if not problems and ok:
                return answer
            feedback = "; ".join(problems + ([f"thesis used numbers not in the dossier: {', '.join(bad)}"] if not ok else []))
        return answer  # type: ignore[return-value]

    def decide(self, as_of, wl, ctx, market, macro, excluded: set[str]) -> list[str]:
        held = positions(self.db)
        nav = float(self.db.scalar("SELECT nav FROM nav WHERE date = :d", {"d": as_of}))
        pending = {r["symbol"] for r in self.db.query("SELECT symbol FROM orders WHERE status = 'pending'")}
        rets = self._returns(list(held) + list(ctx) + ["SPY"], as_of)
        spy = rets.get("SPY", {})
        state = risk.PortfolioState(
            nav=nav,
            weights={s: p["value"] / nav for s, p in held.items()},
            sectors={s: p["sector"] or "Unknown" for s, p in held.items()},
            betas={s: b for s in held if (b := risk.beta(rets.get(s, {}), spy)) is not None},
        )
        regime = market["regime_flags"]

        order = [s for s in ctx if s in held] + [s for s in ctx if s not in held]
        to_review = [s for s in order if s not in excluded and s not in pending and self._needs_review(s, ctx[s], as_of)]
        to_review = to_review[: DECISION["max_reviews_per_day"]]
        dossiers = {s: self._dossier(s, ctx[s], as_of, state, regime, market, held.get(s), DECISION["max_new_positions_per_day"]) for s in to_review}

        def ask(s: str) -> dict[str, Any] | Exception:
            try:
                return self._ask(dossiers[s], s in held)
            except Exception as e:  # returned, not raised, so one failure doesn't cancel the others
                return e

        # reasoning calls are slow (about a minute each), so several run at once; results keep review order
        reviews: list[dict[str, Any]] = []
        budget_hit = None
        with ThreadPoolExecutor(max_workers=DECISION["parallel_reviews"]) as pool:
            for s, answer in zip(to_review, pool.map(ask, to_review)):
                if isinstance(answer, BudgetExceeded):
                    budget_hit = answer
                elif isinstance(answer, Exception):
                    log.warning("decision for %s failed: %s", s, answer)
                    self.notes.append(f"review failed for {s}: {type(answer).__name__}")
                else:
                    reviews.append({"symbol": s, "dossier": dossiers[s], "answer": answer})
        if budget_hit:
            self.alerts.alert("LLM budget cap reached", f"{budget_hit}. No more reviews this month; positions are held as they are.")
        held_unreviewed = [s for s in held if s not in ctx]
        if held_unreviewed:
            self.notes.append(f"held but no price today, not reviewed: {', '.join(held_unreviewed)}")

        # code enforcement: exits first, then buys by confidence through the risk overlay
        buys = 0
        for rv in sorted(reviews, key=lambda r: (r["answer"]["action"] != "EXIT", -(r["answer"]["confidence"] or 0))):
            a, s = rv["answer"], rv["symbol"]
            sector = ctx[s]["profile"]["sector"]
            grounded = a["checks"]["number_grounding"] == "pass" and a["checks"]["schema"] == "pass"
            rv["risk"], rv["required"] = {}, None
            if a["action"] == "EXIT":
                rv["outcome"] = "EXECUTED" if a["checks"]["schema"] == "pass" else "REJECTED_SCHEMA"
            elif a["action"] == "ACCUMULATE":
                req, why = risk.required_confidence(sector, state, regime)
                rv["required"] = req
                if not grounded:
                    rv["outcome"] = "REJECTED_GROUNDING" if a["checks"]["schema"] == "pass" else "REJECTED_SCHEMA"
                elif (a["confidence"] or 0) < req:
                    rv["outcome"] = "SKIP_CONFIDENCE"
                    rv["risk"] = {"required_confidence": req, "reasons": why}
                elif buys >= DECISION["max_new_positions_per_day"]:
                    rv["outcome"] = "SKIP_DAILY_LIMIT"
                else:
                    ok, details = risk.check_buy(s, sector, a["target_allocation"], risk.beta(rets.get(s, {}), spy), state, rets)
                    details["required_confidence"], details["reasons"] = req, why
                    rv["risk"] = details
                    if ok:
                        rv["outcome"] = "EXECUTED"
                        state.weights[s], state.sectors[s] = a["target_allocation"], sector
                        if (b := risk.beta(rets.get(s, {}), spy)) is not None:
                            state.betas[s] = b
                        buys += 1
                    else:
                        rv["outcome"] = "SKIP_RISK"
            else:
                rv["outcome"] = "NO_TRADE"
        return self._record_decisions(as_of, reviews, market)

    def _record_decisions(self, as_of: str, reviews: list[dict[str, Any]], market: dict[str, Any]) -> list[str]:
        if not reviews:
            self.notes.append("decisions: no names needed review")
            return []
        versions = {
            "code_version": self.s.code_version,
            "framework": FRAMEWORK_VERSION,
            "rubric_version": RUBRIC_VERSION,
            "principles_version": PRINCIPLES_VERSION,
            "decision_prompt_version": DECISION_PROMPT_VERSION,
            "models": {"scorer": self.s.scoring_model, "decision": self.s.decision_model if not self.llm.fake else "fake"},
        }
        snapshot = {
            "as_of": as_of,
            **versions,
            "framework_params": FRAMEWORK,
            "decision_params": DECISION,
            "risk_params": RISK,
            "market": _r(market),
            "inputs": {
                rv["symbol"]: {"dossier": rv["dossier"], "answer": rv["answer"], "facts": self._pit_subset(rv["symbol"], as_of)}
                for rv in reviews
            },
        }
        digest = save_snapshot(self.db, as_of, snapshot)
        resolve_on = (date.fromisoformat(as_of) + timedelta(days=round(EVALUATION["decision_horizon"] * 365 / 252))).isoformat()
        out, decision_rows, order_rows = [], [], []
        for rv in reviews:
            s, a, ds = rv["symbol"], rv["answer"], rv["dossier"]
            decision_id = f"{as_of}-{s}"
            executed = rv["outcome"] == "EXECUTED"
            record = {
                "decision_id": decision_id,
                "as_of": f"{as_of}T21:00:00Z",
                "snapshot_hash": digest,
                **versions,
                "ticker": s,
                "name": ds["name"],
                "sector": ds["sector"],
                "held": ds["held"],
                "action": a["action"],
                "outcome": rv["outcome"],
                "target_allocation": a["target_allocation"],
                "confidence": a["confidence"],
                "required_confidence": rv["required"],
                "thesis": a["thesis"],
                "framework_views": a["framework_views"],
                "key_risks": a["key_risks"],
                "lessons_applied": a["lessons_applied"],
                "checks": a["checks"],
                "risk": rv["risk"],
                "dossier_price": ds["price"],
                "computed": {
                    "attention": ds["attention"],
                    "graham": {k: v for k, v in ds["graham"].items() if k != "criteria"},
                    "buffett": {k: v for k, v in ds["buffett"].items() if k != "criteria"},
                    "marks_stock": ds["marks"]["stock"],
                    "market_temperature": ds["marks"]["market"].get("temperature"),
                },
                "resolve_on": resolve_on,
            }
            decision_rows.append(
                {
                    "decision_id": decision_id,
                    "as_of": record["as_of"],
                    "date": as_of,
                    "symbol": s,
                    "action": a["action"],
                    "target_weight": a["target_allocation"],
                    "confidence": a["confidence"],
                    "record": json.dumps(record, default=str),
                    "snapshot_hash": digest,
                    "resolve_on": resolve_on,
                    "thesis_ok": 1 if a["checks"]["number_grounding"] == "pass" else 0,
                    "executed": 1 if executed else 0,
                }
            )
            if executed and a["action"] in ("ACCUMULATE", "EXIT"):
                order_rows.append(
                    {
                        "order_id": decision_id,
                        "decision_id": decision_id,
                        "symbol": s,
                        "side": "BUY" if a["action"] == "ACCUMULATE" else "SELL",
                        "target_weight": a["target_allocation"],
                        "created_date": as_of,
                        "status": "pending",
                        "fill_date": None,
                        "fill_price": None,
                        "amount": None,
                    }
                )
            if a["action"] in ("ACCUMULATE", "EXIT") or rv["outcome"] != "NO_TRADE":
                out.append(f"{a['action']} {s} ({rv['outcome']}, conf {a['confidence']})")
        self.db.upsert("decisions", decision_rows, ["decision_id"], update=False)
        self.db.upsert("orders", order_rows, ["order_id"], update=False)
        holds = sum(1 for rv in reviews if rv["outcome"] == "NO_TRADE")
        self.notes.append(f"decisions: {len(reviews)} reviewed, {holds} hold/pass; " + (", ".join(out) or "no trades"))
        return out

    def _pit_subset(self, symbol: str, as_of: str) -> dict[str, Any]:
        cik = self.db.scalar("SELECT cik FROM symbols WHERE symbol = :s", {"s": symbol})
        if not cik:
            return {}
        rows = self.db.query("SELECT concept, period_end, value, filed FROM facts WHERE cik = :c AND filed <= :d", {"c": cik, "d": as_of})
        pit = graham.point_in_time(rows, as_of)
        keep = graham.EPS + graham.REVENUE + graham.LTD + graham.SHARES + buffett.NET_INCOME + buffett.CFO + buffett.CAPEX + ["AssetsCurrent", "LiabilitiesCurrent", "StockholdersEquity"]
        return {c: pit[c] for c in keep if c in pit}

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
        learning.run_postmortems(self.db, self.llm, panel, as_of, SECTOR_ETFS, self.notes)
        casestudies.write_case_studies(self.db, self.llm, panel, as_of, SECTOR_ETFS, self.notes)
        new_lessons = learning.update_lessons(self.db, as_of)
        m = metrics(self.db)
        ic = evaluation.ic_summary(self.db)
        brier = evaluation.decision_brier(self.db, panel)
        cons = evaluation.consistency_rate(self.db)
        ground = evaluation.grounding_failure_rate(self.db)
        pms = self.db.query("SELECT symbol, case_type, cause, lens, adjustment, explanation FROM postmortems WHERE month = :m", {"m": as_of[:7]})
        lessons = self.db.query("SELECT * FROM lessons WHERE status = 'active' ORDER BY created_month")
        studies = self.db.query("SELECT symbol, checkpoint, verdict, title, excess_spy, excess_sector FROM case_studies WHERE created_at >= :m", {"m": as_of[:7]})

        def ic_line(lens: str) -> str:
            parts = [f"{h}d {v['mean_ic']:+.3f} (t {v['t_stat_overlap_adj']:.1f}, {v['days']} days)" if v["mean_ic"] is not None else f"{h}d – ({v['days']} days)" for h, v in ic[lens].items()]
            return f"- {lens}: " + "; ".join(parts)

        lines = [
            f"# AMIA monthly report: {as_of[:7]}",
            "",
            "## Portfolio (benchmarks: SPY, sector ETFs, T-bills)",
            *(f"- {k}: {v:.4f}" if isinstance(v, float) else f"- {k}: {v}" for k, v in m.items()),
            "",
            "## Which inputs predict returns vs SPY (rank IC by lens)",
            *(ic_line(lens) for lens in ic),
            "",
            "## Calibration of the model's confidence",
            f"- {json.dumps(brier)}",
            "",
            "## Logic checks",
            f"- Thesis grounding: {json.dumps(ground)}",
            f"- Scoring consistency: {json.dumps(cons)}",
            "",
            f"## Post-mortems this month ({len(pms)})",
            *(f"- {p['symbol']} ({p['case_type']}): {p['cause']}; {p['lens']} {p['adjustment']}. {p['explanation']}" for p in pms),
            "",
            f"## Case studies this month ({len(studies)})",
            *(f"- {c['symbol']} ({c['checkpoint']}, {c['verdict']}): {c['title']}. Beat SPY by {c['excess_spy']:+.1%}, sector ETF by {c['excess_sector']:+.1%}." for c in studies),
            "",
            f"## Lessons ({len(lessons)} active, {len(new_lessons)} new)",
            *(f"- [{lsn['sector']} | {lsn['lens']} {lsn['adjustment']}] {lsn['guidance']} ({lsn['n_cases']} cases)" for lsn in lessons),
        ]
        report = "\n".join(lines)
        self.db.set_kv(f"report:{as_of[:7]}", report)
        return report

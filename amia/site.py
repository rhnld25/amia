"""Static dashboard rendered from the database, published to GitHub Pages."""

from __future__ import annotations

import html
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import evaluation
from .config import CORE, EVALUATION, FRAMEWORK_VERSION, PORTFOLIO, PORTFOLIOS, SECTOR_ETFS
from .db import DB
from .portfolio import metrics


def _pct(v: Any, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    if digits >= 0:
        return f"{v * 100:+.{digits}f}%"
    return f"{v * 100:.1f}".rstrip("0").rstrip(".") + "%"  # unsigned, up to one decimal (2.5%, 6%)


def _mos(v: Any) -> str:
    """Graham margin of safety (1 - price / value). Far above value, a multiple reads better than -4731%."""
    if v is None:
        return "–"
    return f"{1 - v:.1f}× value" if v < -1 else _pct(v, 0)


def _num(v: Any, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    return f"{v:,.{digits}f}"


def _e(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


CSS = """
:root{color-scheme:light;--surface-0:#f5f5f3;--surface-1:#fcfcfb;--border:#e4e3df;--grid:#ecebe7;
--text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#7a7974;
--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--series-4:#8a5cd6;--series-5:#c2408a;--series-6:#b38600;--good:#0ca30c;--critical:#d03b3b;--warning:#fab219}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--surface-0:#121211;--surface-1:#1a1a19;
--border:#2e2e2c;--grid:#262624;--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#8f8e86;
--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#9b72e6;--series-5:#d4569c;--series-6:#c99a14}}
:root[data-theme="dark"]{color-scheme:dark;--surface-0:#121211;--surface-1:#1a1a19;--border:#2e2e2c;--grid:#262624;
--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#8f8e86;--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#9b72e6;--series-5:#d4569c;--series-6:#c99a14}
*{box-sizing:border-box}body{margin:0;background:var(--surface-0);color:var(--text-primary);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 12px}
.sub{color:var(--text-secondary);margin:0 0 20px;font-size:14px}
.updated{margin:0 0 8px;font-size:14px;font-weight:600;font-variant-numeric:tabular-nums;color:var(--text-primary)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.tile .k{color:var(--text-secondary);font-size:12px}.tile .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .n{color:var(--text-muted);font-size:12px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:16px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th{text-align:left;color:var(--text-secondary);font-weight:500;border-bottom:1px solid var(--border);padding:6px 8px;white-space:nowrap}
td{border-bottom:1px solid var(--grid);padding:6px 8px;vertical-align:top}td.r,th.r{text-align:right}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--text-secondary);margin-bottom:8px}
.legend i{display:inline-block;width:14px;height:2px;vertical-align:middle;margin-right:6px}
.chart{position:relative}.chart svg{display:block;width:100%;height:auto}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
padding:8px 10px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);display:none;white-space:nowrap}
.tip b{font-weight:600}.badge{display:inline-block;font-size:11px;padding:1px 6px;border-radius:4px;border:1px solid var(--border)}
.stack details{padding:6px 0;border-bottom:1px solid var(--grid)}.stack details:last-child{border-bottom:0}.stack table{margin-top:8px}
.swatch{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
.ok{color:var(--good)}.bad{color:var(--critical)}
ul.thesis{margin:4px 0 0;padding-left:18px;color:var(--text-secondary)}
details summary{cursor:pointer;color:var(--text-secondary)}
pre{white-space:pre-wrap;font-size:12px;color:var(--text-secondary)}
a{color:var(--series-1)}
main.study{max-width:760px}main.study p{max-width:68ch}
table.facts th{text-align:left;color:var(--text-secondary);font-weight:500;white-space:normal;width:34%}table.facts td{white-space:normal}
.takeaway{font-size:16px;border-left:3px solid var(--series-1);padding-left:12px}
"""

CHART_JS = """
(function(){
const data=JSON.parse(document.getElementById('nav-data').textContent);
const root=document.getElementById('nav-chart');if(!data.dates.length){root.innerHTML='<p class="sub">No history yet.</p>';return;}
const W=1000,H=320,L=48,R=150,T=12,B=28;const series=data.series;
let lo=Infinity,hi=-Infinity;series.forEach(sr=>sr.values.forEach(v=>{if(v!==null){lo=Math.min(lo,v);hi=Math.max(hi,v)}}));
const pad=(hi-lo)*0.08||1;lo-=pad;hi+=pad;const n=data.dates.length;
const x=i=>L+(n<2?0:i*(W-L-R)/(n-1)),y=v=>T+(hi-v)*(H-T-B)/(hi-lo);
let s=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Growth of 100: each portfolio vs SPY vs T-bills">`;
for(let k=0;k<=4;k++){const v=lo+(hi-lo)*k/4;s+=`<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/>`+
`<text x="${L-6}" y="${y(v)+4}" text-anchor="end" font-size="11" fill="var(--text-muted)">${v.toFixed(0)}</text>`;}
[0,Math.floor((n-1)/2),n-1].forEach(i=>{s+=`<text x="${x(i)}" y="${H-8}" text-anchor="middle" font-size="11" fill="var(--text-muted)">${data.dates[i]}</text>`;});
series.forEach(sr=>{let d='',pen='M';sr.values.forEach((v,i)=>{if(v===null){pen='M';return;}d+=pen+x(i).toFixed(1)+','+y(v).toFixed(1);pen='L';});
s+=`<path d="${d}" fill="none" stroke="${sr.color}" stroke-width="${sr.bench?1.5:2}" ${sr.bench?'stroke-dasharray="5 4"':''} stroke-linejoin="round"/>`;});
// direct end labels, nudged apart so they never overlap
const labels=series.map(sr=>({sr,y:y(sr.values[n-1])})).sort((a,b)=>a.y-b.y);
for(let i=1;i<labels.length;i++){if(labels[i].y-labels[i-1].y<14)labels[i].y=labels[i-1].y+14;}
labels.forEach(l=>{s+=`<text x="${W-R+8}" y="${l.y+4}" font-size="11" fill="var(--text-secondary)">${l.sr.short} ${l.sr.values[n-1].toFixed(1)}</text>`;});
s+=`<line id="xh" y1="${T}" y2="${H-B}" stroke="var(--text-muted)" stroke-dasharray="3 3" visibility="hidden"/>`;
series.forEach((sr,j)=>{s+=`<circle id="dot-${j}" r="4" fill="${sr.color}" stroke="var(--surface-1)" stroke-width="2" visibility="hidden"/>`;});
s+=`<rect x="${L}" y="${T}" width="${W-L-R}" height="${H-T-B}" fill="transparent" id="hit"/></svg><div class="tip" id="tip"></div>`;
root.innerHTML=s;const svg=root.querySelector('svg'),tip=root.querySelector('#tip'),xh=root.querySelector('#xh');
function show(ev){const r=svg.getBoundingClientRect();const px=(ev.clientX-r.left)*W/r.width;
let i=Math.round((px-L)*(n-1)/(W-L-R));i=Math.max(0,Math.min(n-1,i));xh.setAttribute('x1',x(i));xh.setAttribute('x2',x(i));xh.setAttribute('visibility','visible');
let h=`<b>${data.dates[i]}</b>`;series.forEach((sr,j)=>{const dot=root.querySelector('#dot-'+j),v=sr.values[i];
if(v===null){dot.setAttribute('visibility','hidden');return;}dot.setAttribute('cx',x(i));dot.setAttribute('cy',y(v));dot.setAttribute('visibility','visible');
h+=`<div><span style="color:${sr.color}">●</span> ${sr.label}: ${v.toFixed(2)}</div>`;});tip.innerHTML=h;tip.style.display='block';
const tx=x(i)*r.width/W;tip.style.left=Math.min(tx+12,r.width-tip.offsetWidth-4)+'px';tip.style.top='8px';}
function hide(){tip.style.display='none';xh.setAttribute('visibility','hidden');series.forEach((sr,j)=>root.querySelector('#dot-'+j).setAttribute('visibility','hidden'));}
root.querySelector('#hit').addEventListener('pointermove',show);root.querySelector('#hit').addEventListener('pointerleave',hide);
})();
"""


STRATEGY_LABEL = {
    "long_only": "buys only",
    "long_short": "buys and shorts",
    "long_hedge": "buys, hedged",
    "long_short_hedge": "buys and shorts, hedged",
}
PORTFOLIO_COLOR = ["var(--series-1)", "var(--series-4)", "var(--series-5)", "var(--series-6)"]


def _nav_series(db: DB) -> dict[str, Any]:
    """Growth of 100 for every portfolio from its own first day, with SPY and T-bills from the first portfolio's."""
    rows = db.query("SELECT portfolio_id, date, nav, spy_ret, rf_ret FROM nav ORDER BY date")
    dates = sorted({r["date"] for r in rows})
    out: dict[str, Any] = {"dates": dates, "series": []}
    if not dates:
        return out
    by_pid: dict[str, dict[str, float]] = {}
    market: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_pid.setdefault(r["portfolio_id"], {})[r["date"]] = r["nav"]
        market.setdefault(r["date"], r)
    for i, pid in enumerate(p for p in PORTFOLIOS if p in by_pid):
        vals = [round(by_pid[pid][d] / PORTFOLIO["starting_nav"] * 100, 3) if d in by_pid[pid] else None for d in dates]
        name = PORTFOLIOS[pid]["name"]
        out["series"].append({"key": pid, "label": name, "short": PORTFOLIOS[pid].get("short", name), "color": PORTFOLIO_COLOR[i % len(PORTFOLIO_COLOR)], "values": vals})
    spy = cash = 100.0
    spy_v, cash_v = [], []
    for i, d in enumerate(dates):
        if i:
            spy *= 1 + (market[d]["spy_ret"] or 0)
            cash *= 1 + (market[d]["rf_ret"] or 0)
        spy_v.append(round(spy, 3))
        cash_v.append(round(cash, 3))
    out["series"] += [
        {"key": "spy", "label": "SPY", "short": "SPY", "color": "var(--series-2)", "values": spy_v, "bench": True},
        {"key": "cash", "label": "T-bills (cash)", "short": "Cash", "color": "var(--series-3)", "values": cash_v, "bench": True},
    ]
    return out


def _sector_etf_returns(db: DB, start: str | None, end: str | None) -> list[tuple[str, str, float | None]]:
    out = []
    for sector, etf in SECTOR_ETFS.items():
        ret = None
        if start and end:
            vals = [r["ret"] for r in db.query("SELECT ret FROM prices WHERE symbol = :s AND date > :a AND date <= :b", {"s": etf, "a": start, "b": end}) if r["ret"] is not None]
            if vals:
                level = 1.0
                for v in vals:
                    level *= 1 + v
                ret = level - 1
        out.append((etf, sector, ret))
    return out


OUTCOME_LABEL = {
    "EXECUTED": "executed",
    "NO_TRADE": "no trade",
    "SKIP_CONFIDENCE": "below required confidence",
    "SKIP_RISK": "blocked by risk limits",
    "SKIP_DAILY_LIMIT": "daily limit on new positions reached",
    "REJECTED_GROUNDING": "rejected: thesis numbers not in data",
    "REJECTED_SCHEMA": "rejected: malformed answer",
}

LENS_LABEL = {
    "llm_conf": "Model confidence",
    "attention": "News attention",
    "graham_mos": "Graham margin of safety",
    "buffett_passed": "Buffett quality criteria",
    "marks_pendulum": "Marks pendulum (greed +)",
    "mom_12_1": "12-1 month momentum",
}


def _industry(row: dict[str, Any]) -> str:
    return f"<div class='sub'>{_e(row['industry'])}</div>" if row.get("industry") else ""


def _live_watch(db: DB, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Intraday watchlist rows, with each name's latest framework numbers and review (from the last close)."""
    syms = [r["symbol"] for r in rows]
    if not syms:
        return []
    names = {f"s{i}": s for i, s in enumerate(syms)}
    clause = "(" + ", ".join(f":{k}" for k in names) + ")"
    info = {r["symbol"]: r for r in db.query(f"SELECT symbol, name, sector, industry FROM symbols WHERE symbol IN {clause}", names)}
    feats = {
        r["symbol"]: r
        for r in db.query(
            "SELECT f.symbol, f.graham_passed, f.graham_mos, f.buffett_passed, f.marks_pendulum, f.llm_action, f.llm_conf, f.price "
            f"FROM features f WHERE f.symbol IN {clause} AND f.date = (SELECT MAX(g.date) FROM features g WHERE g.symbol = f.symbol)",
            names,
        )
    }
    framework = ("graham_passed", "graham_mos", "buffett_passed", "marks_pendulum", "price")
    blank = dict.fromkeys(framework + ("llm_action", "llm_conf"))
    out = []
    for r in rows:
        row = {**r, "name": None, "sector": None, "industry": None, **info.get(r["symbol"], {}), **blank, **feats.get(r["symbol"], {})}
        row.update({k: r[k] for k in framework if r.get(k) is not None})  # computed in the refresh, at the last close
        out.append(row)
    return out


ROLE_LABEL = {"drove_result": "Drove the result", "supported": "Supported", "noise": "Noise", "misleading": "Misleading"}
VERDICT_NOTE = {
    "earned": "Earned: the signals the thesis relied on played out.",
    "lucky": "Lucky: it made money for a reason the thesis didn't name. Worth reading as what not to copy.",
}


def _study_slug(study_id: str) -> str:
    return study_id.replace(":", "-")


def _study_page(row: dict[str, Any]) -> str:
    rec = json.loads(row["record"])
    cs, case = rec["study"], rec["case"]
    o, dec = case["outcome"], case.get("decision") or {}
    facts = [
        ("Ticker", f"{row['symbol']} · {row['sector']}"),
        ("Bought", f"{o['decided']} at {_pct(dec.get('target_allocation'), -1)} of the portfolio, confidence {_num(dec.get('confidence'))}"),
        ("Checkpoint", f"{o['checkpoint_date']} ({'position closed' if row['checkpoint'] == 'closed' else 'three months on'}), {o['trading_days_held']} trading days"),
        ("Return", _pct(o["return"])),
        ("vs SPY", _pct(o["excess_vs_spy"])),
        (f"vs sector ETF ({o.get('sector_etf') or '–'})", _pct(o["excess_vs_sector_etf"])),
    ]
    facts_html = "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in facts)
    thesis = "".join(f"<li>{_e(b)}</li>" for b in dec.get("thesis", []))
    signals = "".join(
        f"<tr><td>{_e(x['signal'])}</td><td><span class='badge {'ok' if x['role'] == 'drove_result' else 'bad' if x['role'] == 'misleading' else ''}'>"
        f"{_e(ROLE_LABEL.get(x['role'], x['role']))}</span></td><td>{_e(x['evidence'])}</td></tr>"
        for x in cs["signals"]
    )
    verdict = cs["verdict"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(cs['title'])} · AMIA case study</title><style>{CSS}</style></head><body><main class="study">
<p class="sub"><a href="../index.html#case-studies">← AMIA case studies</a></p>
<h1>{_e(cs['title'])}</h1>
<p><span class="badge {'ok' if verdict == 'earned' else 'bad'}">{_e(verdict.title())}</span> <span class="sub">{_e(VERDICT_NOTE[verdict])}</span></p>
<div class="card"><table class="facts">{facts_html}</table></div>
<h2>The setup</h2><p>{_e(cs['setup'])}</p>
<h2>The call</h2><p>{_e(cs['call'])}</p><p class="sub">The thesis as written on the day:</p><ul class="thesis">{thesis}</ul>
<h2>What happened</h2><p>{_e(cs['what_happened'])}</p>
<h2>Which signals mattered</h2><div class="card"><table><tr><th>Signal</th><th>Role</th><th>Evidence</th></tr>{signals}</table></div>
<h2>Takeaway</h2><p class="takeaway">{_e(cs['takeaway'])}</p>
<details><summary>full decision record</summary><pre>{_e(json.dumps(dec, indent=1))}</pre></details>
<p class="sub">Written by Qwen (via OpenRouter) from the decision's saved inputs and what followed; every number is checked against that data. Paper trading only, not investment advice.</p>
</main></body></html>"""


def build_site(db: DB, out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    m = metrics(db, CORE)
    ic = evaluation.ic_summary(db)
    cons = evaluation.consistency_rate(db)
    ground = evaluation.grounding_failure_rate(db)
    nav_data = _nav_series(db)
    last_date = db.scalar("SELECT MAX(date) FROM watchlist")
    pos = db.query("SELECT p.*, s.industry FROM positions p LEFT JOIN symbols s ON s.symbol = p.symbol ORDER BY p.portfolio_id, p.kind DESC, p.side, p.value DESC")
    pm = {pid: metrics(db, pid) for pid in PORTFOLIOS}
    nav_of = {pid: pm[pid].get("nav") or PORTFOLIO["starting_nav"] for pid in PORTFOLIOS}
    decisions = db.query(
        "SELECT d.portfolio_id, d.record, v.record AS view FROM decisions d LEFT JOIN views v ON v.view_id = d.view_id "
        "WHERE d.action IN ('ACCUMULATE', 'EXIT', 'SHORT', 'COVER') ORDER BY d.date DESC, d.symbol, d.portfolio_id LIMIT 60"
    )
    review_counts = db.query("SELECT portfolio_id, action, COUNT(*) AS n FROM decisions GROUP BY portfolio_id, action")
    n_views = db.scalar("SELECT COUNT(*) FROM views") or 0
    watch = db.query(
        "SELECT w.*, s.name, s.sector, s.industry, f.graham_passed, f.graham_mos, f.buffett_passed, f.marks_pendulum, f.llm_action, f.llm_conf, f.price "
        "FROM watchlist w LEFT JOIN symbols s ON s.symbol = w.symbol LEFT JOIN features f ON f.symbol = w.symbol AND f.date = w.date "
        "WHERE w.date = :d ORDER BY w.rank IS NULL, w.rank",
        {"d": last_date},
    ) if last_date else []
    official_rank = {w["symbol"]: w["rank"] for w in watch}
    live = db.get_kv("watchlist:live")
    last_close_run = db.scalar("SELECT MAX(finished) FROM runs WHERE kind = 'daily'") or ""
    if live and live["updated"] > last_close_run:
        watch = _live_watch(db, live["rows"])
        updated = datetime.fromisoformat(live["updated"].replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York"))
        watch_note = (
            f"Live: refreshed every 15 minutes in market hours, last at {updated:%b %-d, %-I:%M %p} ET. "
            f"Move is against the list at the {live['since'] or 'first'} close, which the evening review decides on."
        )
    else:
        watch_note = f"As of the {last_date or '–'} close. During market hours it refreshes every 15 minutes."
    # intraday refreshes run every 15 minutes; the run log keeps the daily and monthly runs readable
    runs = db.query("SELECT * FROM runs WHERE kind <> 'intraday' ORDER BY started DESC LIMIT 10")
    studies = db.query("SELECT * FROM case_studies WHERE grounded = 1 ORDER BY checkpoint_date DESC, id")
    (out / "studies").mkdir(exist_ok=True)
    for st in studies:
        (out / "studies" / f"{_study_slug(st['id'])}.html").write_text(_study_page(st))
    study_rows = "".join(
        f"<tr><td>{_e(st['checkpoint_date'])}</td><td><b>{_e(st['symbol'])}</b><div class='sub'>{_e(st['sector'])}</div></td>"
        f"<td><a href='studies/{_study_slug(st['id'])}.html'>{_e(st['title'])}</a></td>"
        f"<td><span class='badge {'ok' if st['verdict'] == 'earned' else 'bad'}'>{_e(st['verdict'].title())}</span></td>"
        f"<td class='r'>{_pct(st['excess_spy'])}</td><td class='r'>{_pct(st['excess_sector'])}</td></tr>"
        for st in studies
    ) or (
        "<tr><td colspan='6' class='sub'>The first case studies appear once buys reach their three-month checkpoint "
        "having beaten both SPY and their benchmark ETF by 5 points.</td></tr>"
    )
    closed = db.query("SELECT * FROM closed_positions WHERE kind = 'stock' ORDER BY closed_date DESC, symbol, portfolio_id LIMIT 30")
    spend = db.query("SELECT SUBSTR(ts, 1, 7) AS month, SUM(usd) AS usd FROM llm_spend GROUP BY SUBSTR(ts, 1, 7) ORDER BY month DESC LIMIT 6")
    reports = db.query("SELECT key, value FROM kv WHERE key LIKE 'report:%' ORDER BY key DESC LIMIT 3")
    lessons = db.query("SELECT * FROM lessons WHERE status = 'active' ORDER BY created_month DESC")
    pms = db.query("SELECT * FROM postmortems ORDER BY created_at DESC LIMIT 15")
    llm_ic = (ic.get("llm_conf") or {}).get(EVALUATION["decision_horizon"]) or {}

    def _beta(v: Any) -> str:
        return _num(v) if v is not None else "–"

    def _book(pid: str) -> str:
        rows = [p for p in pos if p["portfolio_id"] == pid]
        long_w = sum(p["value"] for p in rows if p["side"] == "long") / nav_of[pid]
        short_w = sum(p["value"] for p in rows if p["side"] == "short" and p["kind"] == "stock") / nav_of[pid]
        hedge_w = sum(p["value"] for p in rows if p["kind"] == "hedge") / nav_of[pid]
        parts = [f"{_pct(long_w, -1)} long"] + ([f"{_pct(short_w, -1)} short"] if short_w else []) + ([f"{_pct(hedge_w, -1)} hedged"] if hedge_w else [])
        return " · ".join(parts)

    comp_rows = "".join(
        f"<tr><td><span class='swatch' style='background:{PORTFOLIO_COLOR[i % len(PORTFOLIO_COLOR)]}'></span><b>{_e(cfg['name'])}</b>"
        f"<div class='sub'>{_e(STRATEGY_LABEL.get(cfg['strategy'], cfg['strategy']))}{' · ' + _e(cfg['hedge']) + ' hedge' if cfg.get('hedge') else ''} · since {_e(pm[pid].get('start') or 'next run')}</div></td>"
        f"<td class='r'>${_num(nav_of[pid], 0)}</td><td class='r'>{_pct(pm[pid].get('total_return'))}<div class='sub'>SPY {_pct(pm[pid].get('spy_total_return'))}</div></td>"
        f"<td class='r'>{_pct(pm[pid].get('alpha_ann'))}</td><td class='r'>{_num(pm[pid].get('sharpe'))}</td><td class='r'>{_pct(pm[pid].get('max_drawdown'))}</td>"
        f"<td class='r'>{_beta(pm[pid].get('beta_spy'))}</td><td>{_book(pid)}</td></tr>"
        for i, (pid, cfg) in enumerate(PORTFOLIOS.items())
    )
    llm_tile = ("Model view IC, " + str(EVALUATION["decision_horizon"]) + "d", _num(llm_ic.get("mean_ic"), 3), f"{llm_ic.get('days', 0)} resolved days")

    def _pos_ret(p: dict[str, Any]) -> float | None:
        if p["kind"] == "hedge" or not p["cost"]:
            return None
        return p["value"] / p["cost"] - 1 if p["side"] == "long" else 1 - p["value"] / p["cost"]

    def _pos_row(p: dict[str, Any]) -> str:
        return (
            f"<tr><td><b>{_e(p['symbol'])}</b></td>"
        f"<td>{_e('hedge' if p['kind'] == 'hedge' else p['side'])}</td><td>{_e(p['sector'])}{_industry(p)}</td><td>{_e(p['opened_date'])}</td>"
        f"<td class='r'>{_num(p['entry_price'])}</td><td class='r'>{_num(p['last_close'])}</td>"
            f"<td class='r'>{_pct(p['value'] / nav_of.get(p['portfolio_id'], PORTFOLIO['starting_nav']), -1)}</td><td class='r'>{_pct(_pos_ret(p))}</td></tr>"
        )

    pos_head = "<tr><th>Ticker</th><th>Side</th><th>Sector</th><th>Opened</th><th class='r'>Entry</th><th class='r'>Last</th><th class='r'>Weight</th><th class='r'>Return</th></tr>"
    pos_html = "".join(
        f"<details{' open' if pid == CORE else ''}><summary><b>{_e(cfg['name'])}</b> · {sum(1 for p in pos if p['portfolio_id'] == pid)} positions · {_book(pid)}</summary>"
        f"<table>{pos_head}{''.join(_pos_row(p) for p in pos if p['portfolio_id'] == pid) or '<tr><td colspan=8 class=sub>No open positions yet.</td></tr>'}</table></details>"
        for pid, cfg in PORTFOLIOS.items()
    )

    dec_rows = []
    for row in decisions:
        r = json.loads(row["record"])
        v = json.loads(row["view"]) if row["view"] else {}
        opening = r["action"] in ("ACCUMULATE", "SHORT")
        if v and opening:
            bullets = v.get("thesis", []) + [f"Against: {b}" for b in v.get("counter_case", [])]
        elif r.get("reason"):
            bullets = r["reason"]
        else:  # v2.2 decisions carried their own thesis
            bullets = r.get("thesis", [])
        body = "".join(f"<li>{_e(b)}</li>" for b in bullets)
        views = (v or r).get("framework_views") or {}
        views_html = "".join(f"<li><b>{_e(k.title())}:</b> {_e(x)}</li>" for k, x in views.items() if x)
        lessons_used = (v or r).get("lessons_applied") or []
        outcome = r.get("outcome", "")
        p = r.get("p_outperform", r.get("confidence"))
        bench = v.get("benchmark")
        need = r.get("required") if r.get("required") is not None else r.get("required_confidence")
        need_txt = ("needs ≤ " if r["action"] == "SHORT" else "needs ") + _num(need) if need is not None and opening else ""
        who = r.get("decided_by") or "model"
        dec_rows.append(
            f"<tr><td>{_e(r['as_of'][:10])}</td><td>{_e(PORTFOLIOS.get(row['portfolio_id'], {}).get('name', row['portfolio_id']))}</td>"
            f"<td><b>{_e(r['ticker'])}</b><div class='sub'>{_e(r.get('sector'))}</div></td><td>{_e(r['action'])}<div class='sub'>{_e(who)}</div></td>"
            f"<td><span class='badge {'ok' if outcome == 'EXECUTED' else 'bad'}'>{_e(OUTCOME_LABEL.get(outcome, outcome))}</span></td>"
            f"<td class='r'>{_pct(r.get('target_allocation'), -1) if r.get('target_allocation') else '–'}</td>"
            f"<td class='r'>{_num(p)}<div class='sub'>{_e('vs ' + bench) if bench else ''}{(' · ' if bench and need_txt else '') + need_txt}</div></td>"
            f"<td><ul class='thesis'>{body}</ul>"
            f"<details><summary>framework views{' · lessons applied' if lessons_used else ''}</summary><ul class='thesis'>{views_html}"
            f"{''.join(f'<li><b>Lesson:</b> {_e(x)}</li>' for x in lessons_used)}</ul></details>"
            f"<details><summary>full record</summary><pre>{_e(json.dumps({'decision': r, 'view': v} if v else r, indent=1))}</pre></details></td></tr>"
        )
    dec_html = "".join(dec_rows) or "<tr><td colspan='8' class='sub'>No entries or exits yet.</td></tr>"
    counts: dict[str, dict[str, int]] = {}
    for rc in review_counts:
        counts.setdefault(rc["portfolio_id"], {})[rc["action"]] = rc["n"]
    review_note = f"{n_views} stock views so far, each shared by every portfolio. " + " ".join(
        f"{PORTFOLIOS[pid]['name']}: " + ", ".join(f"{n} {a.lower()}" for a, n in sorted(counts[pid].items()) if a not in ("PASS", "HOLD")) + "."
        for pid in PORTFOLIOS
        if pid in counts and any(a not in ("PASS", "HOLD") for a in counts[pid])
    )

    def _pend(v: Any) -> str:
        return "–" if v is None else {2: "greed ++", 1: "greed", 0: "neutral", -1: "fear", -2: "fear ++"}.get(int(v), str(v))

    def _move(w: dict[str, Any]) -> str:
        before = official_rank.get(w["symbol"])
        if not w["rank"] or not official_rank:
            return "–"
        if not before:
            return "<span class='badge ok'>new</span>"
        d = before - w["rank"]
        return "–" if d == 0 else (f"▲{d}" if d > 0 else f"▼{-d}")

    watch_rows = "".join(
        f"<tr><td class='r'>{_e(w['rank'] or 'held')}</td><td class='r'>{_move(w)}</td><td><b>{_e(w['symbol'])}</b><div class='sub'>{_e(w['name'] or '')}</div></td><td>{_e(w['sector'])}{_industry(w)}</td>"
        f"<td class='r'>{_pct(w['weight'], -1)}</td><td class='r'>{_e(w['items'])}</td><td class='r'>{_num(w['avg_sentiment'], 1)}</td>"
        f"<td class='r'>{_num(w['sentiment_shift'], 1)}</td>"
        f"<td class='r'>{(str(w['graham_passed']) + '/8') if w['graham_passed'] is not None else '–'}</td>"
        f"<td class='r'>{_mos(w['graham_mos'])}</td>"
        f"<td class='r'>{(str(w['buffett_passed']) + '/5') if w['buffett_passed'] is not None else '–'}</td>"
        f"<td>{_pend(w['marks_pendulum'])}</td><td>{_e(w['llm_action'] or '–')}</td><td class='r'>{_num(w['llm_conf'])}</td></tr>"
        for w in watch
    ) or "<tr><td colspan='14' class='sub'>The watchlist fills once news has been scored.</td></tr>"

    ic_rows = "".join(
        f"<tr><td>{_e(LENS_LABEL.get(lens, lens))}</td>"
        + "".join(f"<td class='r'>{_num(v['mean_ic'], 3)}</td><td class='r'>{_num(v['t_stat_overlap_adj'], 1)}</td><td class='r'>{v['days']}</td>" for v in by_h.values())
        + "</tr>"
        for lens, by_h in ic.items()
    )
    ic_head = "".join(f"<th class='r'>IC {h}d</th><th class='r'>t</th><th class='r'>days</th>" for h in EVALUATION["horizons"])

    etf_rows = "".join(
        f"<tr><td>{_e(etf)}</td><td>{_e(sector)}</td><td class='r'>{_pct(ret)}</td></tr>"
        for etf, sector, ret in _sector_etf_returns(db, m.get("start"), m.get("end"))
    )

    closed_rows = "".join(
        f"<tr><td>{_e(PORTFOLIOS.get(c['portfolio_id'], {}).get('name', c['portfolio_id']))}</td><td><b>{_e(c['symbol'])}</b></td><td>{_e(c['side'])}</td><td>{_e(c['opened_date'])}</td><td>{_e(c['closed_date'])}</td>"
        f"<td class='r'>{_pct(c['ret'])}</td><td class='r'>{_pct(c['excess_ret'])}</td><td class='r'>{_pct(c['sector_excess_ret'])}</td>"
        f"<td>{_e(', '.join(json.loads(c['tags'] or '[]')))}</td></tr>"
        for c in closed
    ) or "<tr><td colspan='9' class='sub'>No closed positions yet.</td></tr>"

    lesson_rows = "".join(
        f"<tr><td>{_e(lsn['created_month'])}</td><td>{_e(lsn['sector'])}</td><td>{_e(lsn['lens'])} {_e(lsn['adjustment'].replace('_', ' '))}</td>"
        f"<td>{_e(lsn['guidance'])}</td><td class='r'>{lsn['n_cases']}</td></tr>"
        for lsn in lessons
    ) or "<tr><td colspan='5' class='sub'>No lessons yet. A lesson needs several agreeing post-mortems.</td></tr>"
    pm_rows = "".join(
        f"<tr><td>{_e(p['month'])}</td><td>{_e(p['symbol'])}</td><td>{_e(p['case_type'].replace('_', ' '))}</td><td class='r'>{_pct(p['excess_ret'])}</td>"
        f"<td>{_e(p['cause'].replace('_', ' '))}</td><td>{_e(p['explanation'])}</td></tr>"
        for p in pms
    ) or "<tr><td colspan='6' class='sub'>The first post-mortems run on the 1st of the month after a trade closes.</td></tr>"

    run_rows = "".join(
        f"<tr><td>{_e((r['started'] or '')[:16].replace('T', ' '))}</td><td>{_e(r['as_of'])}</td>"
        f"<td class='{'ok' if r['status'] in ('ok', 'market_closed') else 'bad'}'>{_e(r['status'])}</td>"
        f"<td><details><summary>{len(json.loads(r['notes'] or '[]'))} notes</summary><pre>{_e(chr(10).join(json.loads(r['notes'] or '[]')))}</pre></details></td></tr>"
        for r in runs
    )

    health = [
        ("Thesis grounding failures", _pct(ground["failure_rate"], -1) if ground["failure_rate"] is not None else "–", f"{ground['decisions']} reviews · target < 1%"),
        ("Scoring consistency", _pct(cons["agreement"], -1) if cons["agreement"] is not None else "–", f"{cons['pairs']} re-scored pairs · target ≥ 85%"),
        ("LLM spend this month", f"${_num(spend[0]['usd'] if spend else 0)}", "cap $10"),
        ("Active lessons", str(len(lessons)), f"{len(pms)} recent post-mortems"),
        llm_tile,
    ]
    health_html = "".join(f'<div class="tile"><div class="k">{_e(k)}</div><div class="v">{_e(v)}</div><div class="n">{_e(n)}</div></div>' for k, v, n in health)
    report_html = "".join(f"<details><summary>{_e(r['key'][7:])}</summary><pre>{_e(json.loads(r['value']))}</pre></details>" for r in reports) or "<p class='sub'>The first report is written on the 1st of next month.</p>"

    legend = "".join(
        f"<span><i style='background:{sr['color']}'></i>{_e(sr['label'])}</span>" for sr in nav_data["series"]
    )
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AMIA Paper Portfolios</title><style>{CSS}</style></head><body><main>
<h1>AMIA paper portfolios</h1>
<p class="updated">Last updated {datetime.now(ZoneInfo("America/New_York")):%a %b %-d, %-I:%M %p} ET</p>
<p class="sub">Framework {_e(FRAMEWORK_VERSION)} · data through {_e(last_date or '–')} · watchlist from news traction · views by Qwen (via OpenRouter), shared by every portfolio; each portfolio trades them within its own code-enforced risk limits · paper trading only, not investment advice.</p>
<h2>Portfolios</h2><p class="sub">Each starts at $100,000 and acts on the same daily views of each stock. Entries follow the view by rule; the model decides, position by position, whether each portfolio keeps what it holds.</p>
<div class="card"><table><tr><th>Portfolio</th><th class="r">NAV</th><th class="r">Return</th><th class="r">Alpha (ann.)</th><th class="r">Sharpe</th><th class="r">Max DD</th><th class="r">Beta</th><th>Book</th></tr>{comp_rows}</table></div>
<h2>Growth of 100</h2>
<div class="card"><div class="legend">{legend}</div>
<div class="chart" id="nav-chart"></div></div>
<script type="application/json" id="nav-data">{json.dumps(nav_data)}</script>
<h2>Open positions</h2><div class="card stack">{pos_html}</div>
<h2>Entries and exits</h2><p class="sub">{_e(review_note)} p is the view's probability of beating the stock's benchmark ETF over {EVALUATION["decision_horizon"]} trading days.</p><div class="card"><table><tr><th>Date</th><th>Portfolio</th><th>Ticker</th><th>Action</th><th>Outcome</th><th class="r">Allocation</th><th class="r">p</th><th>Thesis or reason</th></tr>{dec_html}</table></div>
<h2 id="case-studies">Case studies</h2><p class="sub">Winning buys written up as study material: what the signals said, what happened, and whether the thesis earned the result or got lucky. A buy qualifies when it beats both SPY and its benchmark ETF by 5 points, three months on or when it closes.</p>
<div class="card"><table><tr><th>Checkpoint</th><th>Ticker</th><th>Study</th><th>Verdict</th><th class="r">vs SPY</th><th class="r">vs benchmark</th></tr>{study_rows}</table></div>
<h2>Today's watchlist (from news traction)</h2><p class="sub">{_e(watch_note)}</p><div class="card"><table><tr><th class="r">#</th><th class="r">Move</th><th>Ticker</th><th>Sector</th><th class="r">Attention</th><th class="r">Items</th><th class="r">Sentiment</th><th class="r">Shift</th><th class="r">Graham</th><th class="r">MoS</th><th class="r">Buffett</th><th>Marks</th><th>Core action</th><th class="r">p</th></tr>{watch_rows}</table></div>
<h2>Which inputs predict returns vs SPY</h2><p class="sub">Rank correlation between each input and later return against SPY, across the watchlist. Near zero means no predictive value; it takes months to mean anything.</p>
<div class="card"><table><tr><th>Input</th>{ic_head}</tr>{ic_rows}</table></div>
<h2>Sector ETFs over the same period</h2><div class="card"><table><tr><th>ETF</th><th>Sector</th><th class="r">Return</th></tr>{etf_rows}</table></div>
<h2>Closed positions</h2><p class="sub">Returns are the trade's own: a short gains when the stock falls. Excess is signed the same way.</p><div class="card"><table><tr><th>Portfolio</th><th>Ticker</th><th>Side</th><th>Opened</th><th>Closed</th><th class="r">Return</th><th class="r">vs SPY</th><th class="r">vs benchmark</th><th>Attribution</th></tr>{closed_rows}</table></div>
<h2>Lessons</h2><div class="card"><table><tr><th>Since</th><th>Sector</th><th>Adjustment</th><th>Guidance</th><th class="r">Cases</th></tr>{lesson_rows}</table></div>
<h2>Post-mortems</h2><div class="card"><table><tr><th>Month</th><th>Ticker</th><th>Case</th><th class="r">Excess</th><th>Cause</th><th>Explanation</th></tr>{pm_rows}</table></div>
<h2>System health</h2><div class="tiles">{health_html}</div>
<h2>Runs</h2><div class="card"><table><tr><th>Started (UTC)</th><th>As of</th><th>Status</th><th>Notes</th></tr>{run_rows}</table></div>
<h2>Monthly reports</h2><div class="card">{report_html}</div>
</main><script>{CHART_JS}</script></body></html>"""
    (out / "index.html").write_text(page)
    (out / "data.json").write_text(json.dumps({"metrics": pm, "ic": ic, "nav": nav_data}, default=str))

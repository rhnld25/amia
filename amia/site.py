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
from .config import EVALUATION, FRAMEWORK_VERSION, PORTFOLIO, SECTOR_ETFS
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
--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--good:#0ca30c;--critical:#d03b3b;--warning:#fab219}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--surface-0:#121211;--surface-1:#1a1a19;
--border:#2e2e2c;--grid:#262624;--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#8f8e86;
--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70}}
:root[data-theme="dark"]{color-scheme:dark;--surface-0:#121211;--surface-1:#1a1a19;--border:#2e2e2c;--grid:#262624;
--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#8f8e86;--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70}
*{box-sizing:border-box}body{margin:0;background:var(--surface-0);color:var(--text-primary);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 12px}
.sub{color:var(--text-secondary);margin:0 0 20px;font-size:14px}
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
.ok{color:var(--good)}.bad{color:var(--critical)}
ul.thesis{margin:4px 0 0;padding-left:18px;color:var(--text-secondary)}
details summary{cursor:pointer;color:var(--text-secondary)}
pre{white-space:pre-wrap;font-size:12px;color:var(--text-secondary)}
"""

CHART_JS = """
(function(){
const data=JSON.parse(document.getElementById('nav-data').textContent);
const root=document.getElementById('nav-chart');if(!data.dates.length){root.innerHTML='<p class="sub">No history yet.</p>';return;}
const W=1000,H=320,L=48,R=120,T=12,B=28;
const series=[['portfolio','Portfolio','var(--series-1)','Portfolio'],['spy','SPY','var(--series-2)','SPY'],['cash','T-bills (cash)','var(--series-3)','Cash']];
let lo=Infinity,hi=-Infinity;series.forEach(([k])=>data[k].forEach(v=>{lo=Math.min(lo,v);hi=Math.max(hi,v)}));
const pad=(hi-lo)*0.08||1;lo-=pad;hi+=pad;const n=data.dates.length;
const x=i=>L+(n<2?0:i*(W-L-R)/(n-1)),y=v=>T+(hi-v)*(H-T-B)/(hi-lo);
let s=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Growth of 100: portfolio vs SPY vs T-bills">`;
for(let k=0;k<=4;k++){const v=lo+(hi-lo)*k/4;s+=`<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/>`+
`<text x="${L-6}" y="${y(v)+4}" text-anchor="end" font-size="11" fill="var(--text-muted)">${v.toFixed(0)}</text>`;}
[0,Math.floor((n-1)/2),n-1].forEach(i=>{s+=`<text x="${x(i)}" y="${H-8}" text-anchor="middle" font-size="11" fill="var(--text-muted)">${data.dates[i]}</text>`;});
series.forEach(([k,label,c])=>{const d=data[k].map((v,i)=>(i?'L':'M')+x(i).toFixed(1)+','+y(v).toFixed(1)).join('');
s+=`<path d="${d}" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round"/>`;});
// direct end labels, nudged apart so they never overlap
const labels=series.map(([k,,,short])=>({k,short,y:y(data[k][n-1])})).sort((a,b)=>a.y-b.y);
for(let i=1;i<labels.length;i++){if(labels[i].y-labels[i-1].y<14)labels[i].y=labels[i-1].y+14;}
labels.forEach(l=>{s+=`<text x="${W-R+8}" y="${l.y+4}" font-size="11" fill="var(--text-secondary)">${l.short} ${data[l.k][n-1].toFixed(1)}</text>`;});
s+=`<line id="xh" y1="${T}" y2="${H-B}" stroke="var(--text-muted)" stroke-dasharray="3 3" visibility="hidden"/>`;
series.forEach(([k,,c])=>{s+=`<circle id="dot-${k}" r="4" fill="${c}" stroke="var(--surface-1)" stroke-width="2" visibility="hidden"/>`;});
s+=`<rect x="${L}" y="${T}" width="${W-L-R}" height="${H-T-B}" fill="transparent" id="hit"/></svg><div class="tip" id="tip"></div>`;
root.innerHTML=s;const svg=root.querySelector('svg'),tip=root.querySelector('#tip'),xh=root.querySelector('#xh');
function show(ev){const r=svg.getBoundingClientRect();const px=(ev.clientX-r.left)*W/r.width;
let i=Math.round((px-L)*(n-1)/(W-L-R));i=Math.max(0,Math.min(n-1,i));xh.setAttribute('x1',x(i));xh.setAttribute('x2',x(i));xh.setAttribute('visibility','visible');
let h=`<b>${data.dates[i]}</b>`;series.forEach(([k,label,c])=>{const dot=root.querySelector('#dot-'+k);dot.setAttribute('cx',x(i));dot.setAttribute('cy',y(data[k][i]));dot.setAttribute('visibility','visible');
h+=`<div><span style="color:${c}">●</span> ${label}: ${data[k][i].toFixed(2)}</div>`;});tip.innerHTML=h;tip.style.display='block';
const tx=x(i)*r.width/W;tip.style.left=Math.min(tx+12,r.width-tip.offsetWidth-4)+'px';tip.style.top='8px';}
function hide(){tip.style.display='none';xh.setAttribute('visibility','hidden');series.forEach(([k])=>root.querySelector('#dot-'+k).setAttribute('visibility','hidden'));}
root.querySelector('#hit').addEventListener('pointermove',show);root.querySelector('#hit').addEventListener('pointerleave',hide);
})();
"""


def _nav_series(db: DB) -> dict[str, list]:
    rows = db.query("SELECT date, nav, spy_ret, rf_ret FROM nav ORDER BY date")
    out: dict[str, list] = {"dates": [], "portfolio": [], "spy": [], "cash": []}
    spy = cash = 100.0
    for i, r in enumerate(rows):
        if i:
            spy *= 1 + (r["spy_ret"] or 0)
            cash *= 1 + (r["rf_ret"] or 0)
        out["dates"].append(r["date"])
        out["portfolio"].append(round(r["nav"] / PORTFOLIO["starting_nav"] * 100, 3))
        out["spy"].append(round(spy, 3))
        out["cash"].append(round(cash, 3))
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
    "SKIP_DAILY_LIMIT": "daily buy limit reached",
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


def _live_watch(db: DB, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Intraday watchlist rows, with each name's latest framework numbers and review (from the last close)."""
    syms = [r["symbol"] for r in rows]
    if not syms:
        return []
    names = {f"s{i}": s for i, s in enumerate(syms)}
    clause = "(" + ", ".join(f":{k}" for k in names) + ")"
    info = {r["symbol"]: r for r in db.query(f"SELECT symbol, name, sector FROM symbols WHERE symbol IN {clause}", names)}
    feats = {
        r["symbol"]: r
        for r in db.query(
            "SELECT f.symbol, f.graham_passed, f.graham_mos, f.buffett_passed, f.marks_pendulum, f.llm_action, f.llm_conf, f.price "
            f"FROM features f WHERE f.symbol IN {clause} AND f.date = (SELECT MAX(g.date) FROM features g WHERE g.symbol = f.symbol)",
            names,
        )
    }
    blank = dict.fromkeys(("graham_passed", "graham_mos", "buffett_passed", "marks_pendulum", "llm_action", "llm_conf", "price"))
    return [{**r, "name": None, "sector": None, **info.get(r["symbol"], {}), **blank, **feats.get(r["symbol"], {})} for r in rows]


def build_site(db: DB, out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    m = metrics(db)
    ic = evaluation.ic_summary(db)
    cons = evaluation.consistency_rate(db)
    ground = evaluation.grounding_failure_rate(db)
    nav_data = _nav_series(db)
    last_date = db.scalar("SELECT MAX(date) FROM watchlist")
    pos = db.query("SELECT * FROM positions ORDER BY value DESC")
    nav = m.get("nav") or PORTFOLIO["starting_nav"]
    decisions = db.query("SELECT record FROM decisions WHERE action IN ('ACCUMULATE', 'EXIT') ORDER BY date DESC, decision_id LIMIT 40")
    review_counts = db.query("SELECT action, COUNT(*) AS n FROM decisions GROUP BY action")
    watch = db.query(
        "SELECT w.*, s.name, s.sector, f.graham_passed, f.graham_mos, f.buffett_passed, f.marks_pendulum, f.llm_action, f.llm_conf, f.price "
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
    closed = db.query("SELECT * FROM closed_positions ORDER BY closed_date DESC LIMIT 20")
    spend = db.query("SELECT SUBSTR(ts, 1, 7) AS month, SUM(usd) AS usd FROM llm_spend GROUP BY SUBSTR(ts, 1, 7) ORDER BY month DESC LIMIT 6")
    reports = db.query("SELECT key, value FROM kv WHERE key LIKE 'report:%' ORDER BY key DESC LIMIT 3")
    lessons = db.query("SELECT * FROM lessons WHERE status = 'active' ORDER BY created_month DESC")
    pms = db.query("SELECT * FROM postmortems ORDER BY created_at DESC LIMIT 15")
    llm_ic = (ic.get("llm_conf") or {}).get(EVALUATION["decision_horizon"]) or {}

    tiles = [
        ("NAV", f"${_num(nav, 0)}", f"start ${_num(PORTFOLIO['starting_nav'], 0)}"),
        ("Total return", _pct(m.get("total_return")), f"SPY {_pct(m.get('spy_total_return'))} · T-bills {_pct(m.get('cash_total_return'))}"),
        ("Alpha vs SPY (ann.)", _pct(m.get("alpha_ann")), f"beta-adjusted · 95% CI ±{_pct(m.get('alpha_ci95')).lstrip('+')}"),
        ("Sharpe", _num(m.get("sharpe")), f"95% CI ±{_num(m.get('sharpe_ci95'))}"),
        ("Max drawdown", _pct(m.get("max_drawdown")), f"{m.get('days', 0)} trading days"),
        ("Model confidence IC, 126d", _num(llm_ic.get("mean_ic"), 3), f"{llm_ic.get('days', 0)} resolved days"),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="k">{_e(k)}</div><div class="v">{_e(v)}</div><div class="n">{_e(n)}</div></div>' for k, v, n in tiles)

    pos_rows = "".join(
        f"<tr><td>{_e(p['symbol'])}</td><td>{_e(p['sector'])}</td><td>{_e(p['opened_date'])}</td>"
        f"<td class='r'>{_num(p['entry_price'])}</td><td class='r'>{_num(p['last_close'])}</td>"
        f"<td class='r'>{_pct(p['value'] / nav, -1)}</td><td class='r'>{_pct(p['value'] / p['cost'] - 1)}</td></tr>"
        for p in pos
    ) or "<tr><td colspan='7' class='sub'>No open positions yet.</td></tr>"

    dec_rows = []
    for row in decisions:
        r = json.loads(row["record"])
        thesis = "".join(f"<li>{_e(b)}</li>" for b in r.get("thesis", []))
        views = r.get("framework_views") or {}
        views_html = "".join(f"<li><b>{_e(k.title())}:</b> {_e(v)}</li>" for k, v in views.items() if v)
        lessons_used = r.get("lessons_applied") or []
        outcome = r.get("outcome", "")
        ok = outcome == "EXECUTED"
        dec_rows.append(
            f"<tr><td>{_e(r['as_of'][:10])}</td><td><b>{_e(r['ticker'])}</b><div class='sub'>{_e(r.get('sector'))}</div></td><td>{_e(r['action'])}</td>"
            f"<td><span class='badge {'ok' if ok else 'bad'}'>{_e(OUTCOME_LABEL.get(outcome, outcome))}</span></td>"
            f"<td class='r'>{_pct(r['target_allocation'], -1) if r['target_allocation'] else '–'}</td>"
            f"<td class='r'>{_num(r['confidence'])}<div class='sub'>{'needs ' + _num(r['required_confidence']) if r.get('required_confidence') else ''}</div></td>"
            f"<td><ul class='thesis'>{thesis}</ul>"
            f"<details><summary>framework views{' · lessons applied' if lessons_used else ''}</summary><ul class='thesis'>{views_html}"
            f"{''.join(f'<li><b>Lesson:</b> {_e(x)}</li>' for x in lessons_used)}</ul></details>"
            f"<details><summary>full record</summary><pre>{_e(json.dumps(r, indent=1))}</pre></details></td></tr>"
        )
    dec_html = "".join(dec_rows) or "<tr><td colspan='7' class='sub'>No buy or exit decisions yet.</td></tr>"
    counts = {r["action"]: r["n"] for r in review_counts}
    review_note = f"{sum(counts.values())} reviews so far: " + ", ".join(f"{n} {a.lower()}" for a, n in sorted(counts.items())) if counts else "No reviews yet."

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
        f"<tr><td class='r'>{_e(w['rank'] or 'held')}</td><td class='r'>{_move(w)}</td><td><b>{_e(w['symbol'])}</b><div class='sub'>{_e(w['name'] or '')}</div></td><td>{_e(w['sector'])}</td>"
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
        f"<tr><td>{_e(c['symbol'])}</td><td>{_e(c['opened_date'])}</td><td>{_e(c['closed_date'])}</td>"
        f"<td class='r'>{_pct(c['ret'])}</td><td class='r'>{_pct(c['excess_ret'])}</td><td class='r'>{_pct(c['sector_excess_ret'])}</td>"
        f"<td>{_e(', '.join(json.loads(c['tags'] or '[]')))}</td></tr>"
        for c in closed
    ) or "<tr><td colspan='7' class='sub'>No closed positions yet.</td></tr>"

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
    ]
    health_html = "".join(f'<div class="tile"><div class="k">{_e(k)}</div><div class="v">{_e(v)}</div><div class="n">{_e(n)}</div></div>' for k, v, n in health)
    report_html = "".join(f"<details><summary>{_e(r['key'][7:])}</summary><pre>{_e(json.loads(r['value']))}</pre></details>" for r in reports) or "<p class='sub'>The first report is written on the 1st of next month.</p>"

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AMIA Paper Portfolio</title><style>{CSS}</style></head><body><main>
<h1>AMIA paper portfolio</h1>
<p class="sub">Framework {_e(FRAMEWORK_VERSION)} · data through {_e(last_date or '–')} · watchlist from news traction · decisions by Qwen (via OpenRouter) within code-enforced risk limits · paper trading only, not investment advice.</p>
<div class="tiles">{tiles_html}</div>
<h2>Growth of 100</h2>
<div class="card"><div class="legend"><span><i style="background:var(--series-1)"></i>Portfolio</span><span><i style="background:var(--series-2)"></i>SPY</span><span><i style="background:var(--series-3)"></i>T-bills (cash)</span></div>
<div class="chart" id="nav-chart"></div></div>
<script type="application/json" id="nav-data">{json.dumps(nav_data)}</script>
<h2>Open positions</h2><div class="card"><table><tr><th>Ticker</th><th>Sector</th><th>Opened</th><th class="r">Entry</th><th class="r">Last</th><th class="r">Weight</th><th class="r">Return</th></tr>{pos_rows}</table></div>
<h2>Buy and exit decisions</h2><p class="sub">{_e(review_note)}</p><div class="card"><table><tr><th>Date</th><th>Ticker</th><th>Action</th><th>Outcome</th><th class="r">Allocation</th><th class="r">Confidence</th><th>Thesis</th></tr>{dec_html}</table></div>
<h2>Today's watchlist (from news traction)</h2><p class="sub">{_e(watch_note)}</p><div class="card"><table><tr><th class="r">#</th><th class="r">Move</th><th>Ticker</th><th>Sector</th><th class="r">Attention</th><th class="r">Items</th><th class="r">Sentiment</th><th class="r">Shift</th><th class="r">Graham</th><th class="r">MoS</th><th class="r">Buffett</th><th>Marks</th><th>Last review</th><th class="r">Conf.</th></tr>{watch_rows}</table></div>
<h2>Which inputs predict returns vs SPY</h2><p class="sub">Rank correlation between each input and later return against SPY, across the watchlist. Near zero means no predictive value; it takes months to mean anything.</p>
<div class="card"><table><tr><th>Input</th>{ic_head}</tr>{ic_rows}</table></div>
<h2>Sector ETFs over the same period</h2><div class="card"><table><tr><th>ETF</th><th>Sector</th><th class="r">Return</th></tr>{etf_rows}</table></div>
<h2>Closed positions</h2><div class="card"><table><tr><th>Ticker</th><th>Opened</th><th>Closed</th><th class="r">Return</th><th class="r">vs SPY</th><th class="r">vs sector ETF</th><th>Attribution</th></tr>{closed_rows}</table></div>
<h2>Lessons</h2><div class="card"><table><tr><th>Since</th><th>Sector</th><th>Adjustment</th><th>Guidance</th><th class="r">Cases</th></tr>{lesson_rows}</table></div>
<h2>Post-mortems</h2><div class="card"><table><tr><th>Month</th><th>Ticker</th><th>Case</th><th class="r">vs SPY</th><th>Cause</th><th>Explanation</th></tr>{pm_rows}</table></div>
<h2>System health</h2><div class="tiles">{health_html}</div>
<h2>Runs</h2><div class="card"><table><tr><th>Started (UTC)</th><th>As of</th><th>Status</th><th>Notes</th></tr>{run_rows}</table></div>
<h2>Monthly reports</h2><div class="card">{report_html}</div>
</main><script>{CHART_JS}</script></body></html>"""
    (out / "index.html").write_text(page)
    (out / "data.json").write_text(json.dumps({"metrics": m, "ic": ic, "nav": nav_data}, default=str))

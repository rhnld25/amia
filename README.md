# AMIA: Autonomous Market Intelligence Analyst

AMIA is a paper-trading research system. It screens the S&P 1500 every weekday with Graham's defensive rules, using point-in-time SEC data. It scores new filings and news against a fixed rubric with Claude Haiku 5.5, and it manages a simulated portfolio with hard risk limits. It never places real trades.

The design is in [AMIA_spec_v2.1.md](AMIA_spec_v2.1.md). To deploy it for free on GitHub Actions + Supabase + GitHub Pages, follow [SETUP.md](SETUP.md).

## How a day runs

1. **Data.** It refreshes index membership (monthly), daily prices (Alpaca, full-market feed), macro series (FRED) and new EDGAR filings for every company, plus XBRL fundamentals when a 10-K or 10-Q lands.
2. **Quality gates.** It halts decisions if more than 2% of prices are missing. It excludes a stock that moves 50% or more in a day, and it alerts on stale yields.
3. **Text scoring.** New filings (MD&A sections) and news are scored 0–4 for novelty, sentiment and materiality by Claude Haiku 5.5. It uses the Batch API (50% off) under a hard $10/month cap. 5% of items are scored twice to measure consistency.
4. **Fills.** Yesterday's orders fill at today's open plus slippage, and every position is marked to today's close.
5. **Scores.** Every name gets a composite score (value, quality and text, each on a 0–4 scale), computed entirely in code.
6. **Decisions.** It exits holdings that reach fair value or whose fundamentals break. It buys up to 3 new names a day that pass the screen with a 25%+ margin of safety, if the risk overlay approves them. Each decision is saved with a content-hashed snapshot of its inputs.
7. **Thesis.** The LLM writes three bullets. Every number must match a computed value, or the thesis is replaced by a deterministic template and flagged.
8. **Evaluation.** It computes the daily rank IC at 21 and 126 trading days, calibration, Brier scores and attribution tags for closed positions.
9. **Dashboard.** It rebuilds the static page and publishes it to GitHub Pages.

## Local use

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

Run the tests:

```bash
.venv/bin/python -m pytest -q
```

Replay 16 months on the synthetic market (no keys needed):

```bash
AMIA_MOCK=1 .venv/bin/python -m amia simulate --start 2024-03-01 --end 2025-06-30
```

Render the dashboard to `./site`:

```bash
AMIA_MOCK=1 .venv/bin/python -m amia build-site
```

Commands: `daily [--date]`, `backfill [--limit]`, `monthly`, `build-site`, `simulate` (mock only), `init-db`. Without `DATABASE_URL` the database is a local SQLite file, `amia_local.db`.

## Code map

| Path | What it does |
|---|---|
| `amia/data/` | EDGAR, FRED, Alpaca and Wikipedia clients; synthetic `mock.py` for tests |
| `amia/frameworks/graham.py` | Point-in-time facts, the 8-point defensive screen, intrinsic value with its sensitivity band |
| `amia/llm.py` | Rubric scoring (batched), thesis writing, spend tracking and cap |
| `amia/rubrics/` | Versioned scoring rubric |
| `amia/risk.py` | Regime flags, crowding threshold, position, sector, beta and correlation limits |
| `amia/decision.py` | Composite score, confidence, number-grounding check |
| `amia/portfolio.py` | Fill model, daily marking, performance metrics with intervals |
| `amia/evaluation.py` | Rank IC, calibration, Brier, attribution tags, learning gate |
| `amia/pipeline.py` | Daily orchestration |
| `amia/site.py` | Dashboard |

## Not built yet

- **Automated parameter-update proposals (spec Phase 5).** The learning gate and its reports are in place. Changes to the framework are made by hand as a new `FRAMEWORK_VERSION`.
- **Buffett-style quality and Marks-style cycle frameworks.** These are post-MVP in the spec.

# Autonomous Market Intelligence Analyst (AMIA) v2.1

*Revision of v2.0. The main changes: snapshots that record what was known at each decision time, sizing and confidence done entirely in code, measurement on many predictions instead of 12-month P&L, and a learning loop that stays off until the data can support it.*

## Mission

AMIA is a continuous buy-side research platform. It answers three questions:

1. Which market developments within a defined universe deserve attention?
2. What do explicit, coded investment frameworks recommend, given deterministic math plus scored qualitative context?
3. Which framework parameters should change, based on evidence large enough to separate skill from noise?

## Claimed edge (to be tested, not assumed)

AMIA uses the same public data as everyone else. Its hypothesized edges are:

- **Breadth.** It applies the same disciplined screen every day to roughly 1,500 names, including small and mid caps that get less analyst coverage.
- **Discipline.** It never skips a rule, and its process is identical across names and across time.
- **Speed on filings.** It scores new 10-K, 10-Q and 8-K filings within hours of their release on EDGAR.

Each edge has its own measurement (see Success Criteria). If none shows up after 12 months, that is a result in itself.

## Design principles

1. **The LLM never produces a number that drives a decision.** Scores, confidence and position size come from code. The LLM classifies text against fixed rubrics and writes prose.
2. **Every decision can be reproduced.** Each decision is stored as an immutable snapshot: the inputs, their as-of timestamps, the code version, the prompt and the pinned model version.
3. **Validation runs forward only.** The LLM already knows how past events turned out, and aggregator fundamentals are often restated. Historical backtests therefore only check that the plumbing works. They are never evidence of edge.
4. **Many small predictions beat a few big ones.** AMIA measures itself on daily rankings of the whole watchlist, not just on the trades it takes.
5. **Plain code wherever possible.** Orchestration, risk limits and attribution are deterministic code, not agents.

## Architecture

```text
            Scheduler + Monitor (cron, plain code, alerts)
                              │
   ┌──────────────┬───────────┼─────────────┬──────────────────┐
   ▼              ▼           ▼             ▼                  ▼
 Data &       Framework    Macro &      Decision         Evaluation &
 Snapshot     Engine       Risk         Engine           Learning
 Layer        (code+LLM    Overlay      (code; LLM       (metrics,
              rubrics)     (code)       writes prose)     calibration,
   │              │           │             │             gated updates)
   └──────────────┴─────┬─────┴─────────────┘                  │
                        ▼                                      │
              Decision Log (append-only) ──► Paper Portfolio ──┘
```

## Universe and benchmarks

- **Universe:** the S&P Composite 1500. Membership is snapshotted every month. Names that drop out stay in the evaluation set (no survivorship bias).
- **Primary benchmark:** an equal-weighted universe return. It matches how the screen treats names.
- **Secondary benchmarks:** SPY, sector ETFs (SPDR XL* family), and the 3-month T-bill (risk-free).

---

## Component 1: Data & Snapshot Layer

**Purpose:** fetch reliable, machine-readable data and freeze exactly what was known at each decision time.

| Data | Source | Cost |
|---|---|---|
| Fundamentals | SEC EDGAR XBRL APIs (`companyfacts`, `frames`). Each figure is tied to its filing's accepted date. | Free |
| Filing text (MD&A, risk factors, 8-K items) | EDGAR full-text filings | Free |
| Daily prices and corporate actions | Alpaca Market Data, Basic plan. Uses the all-exchange (SIP) feed, which the free plan serves for data older than 15 minutes; the post-close run is always outside that window. | Free |
| News | Alpaca News API (Benzinga-sourced, history back to 2015) | Free on Basic as of last check; confirm |
| Universe membership | Wikipedia's S&P 500, 400 and 600 constituent tables via the MediaWiki API, refreshed monthly; CIKs from the SEC ticker map. (iShares holdings files block automated downloads.) | Free |
| Macro | FRED: Treasury curve, effective fed funds, credit spreads, breakevens | Free |

**Rules:**

- **Fundamentals:**
  - Each fundamental figure becomes usable only from its filing's `accepted` timestamp onward, never from the period end date.
  - Restatements are stored as new versions and never overwrite the original figures.
- **Snapshots:**
  - Every daily run writes a content-hashed snapshot of every input it used.
  - Every decision record points to its snapshot hash.
- **Data-quality gates:** a run halts and alerts if any of these fail:
  - Missing prices for more than 2% of the universe.
  - Stale data older than one trading day.
  - A one-day move of 50% or more in split- and dividend-adjusted prices. This one doesn't halt the run: the affected stock is excluded from that day's decisions and an alert is raised.
- **Filing detection:** every run checks EDGAR's submissions feed for each universe company, so same-day filings are seen. A new 10-K or 10-Q triggers a refresh of that company's XBRL facts.

**Text scoring (fast-tier LLM):**

- New filings and news are scored against fixed rubrics. Each dimension gets an integer from 0 to 4, with a defined anchor for every level:
  - **Novelty:** does this contradict or extend prior disclosures?
  - **Sentiment direction**
  - **Materiality:** does it touch revenue, margins, the balance sheet, or management?
- The output is structured JSON validated against a schema. Invalid output is retried once, then recorded as missing. It is never imputed.
- **Consistency check:** 5% of items are re-scored every day. Agreement between the two scores is tracked as a metric.

**Output:** a daily watchlist ranked by a composite score, computed in code from the fundamental factors and the rubric scores.

---

## Component 2: Framework Engine

**Purpose:** encode investment frameworks as executable, versioned logic.

**Hard-coded math (deterministic, unit-tested):**

- **Graham Defensive Screen.** The MVP framework. It checks:
  - Size floor: annual revenue ≥ $500M.
  - Current ratio ≥ 2.
  - Long-term debt ≤ net current assets.
  - Positive earnings in each of the last 10 years.
  - Uninterrupted dividends for the last 10 fiscal years (XBRL data only goes back to about 2009).
  - 10-year EPS growth ≥ 33%.
  - P/E ≤ 15 on 3-year average earnings.
  - P/B × P/E ≤ 22.5.
  - A stock passes the screen when it meets at least 7 of these 8 checks. Financials usually fail the balance-sheet checks, as in Graham's original.
- **Graham intrinsic value**, `V = EPS × (8.5 + 2g) × 4.4 / Y`:
  - `g` = 5-year EPS CAGR from EDGAR data, capped at the range [0%, 15%]. It never comes from analyst estimates or an LLM.
  - `Y` = the current AAA corporate yield from FRED.
  - A sensitivity band is always reported: `V` at `g ± 2pp` and `Y ± 50bp`. Margin of safety counts only if it holds at the low end of the band.
- **Quality screen (post-MVP, Buffett-inspired):** 10-year median ROIC, gross-margin stability (coefficient of variation), net debt / EBITDA, and owner-earnings yield.
- **Cycle positioning (post-MVP, Marks-inspired):** this lives in the Macro & Risk Overlay (Component 3), not in stock valuation.

**Qualitative rubrics (LLM, scored, not retrieved):**

- Subjective dimensions such as moat durability, management candor and supply-chain fragility are scored 0–4 against written rubrics.
- Each rubric is a versioned file containing anchors and examples.
- Every rubric score must cite the source passage it relied on (filing and section).
- A vector store is optional. It is used only to fetch the relevant filing passages, not quotes from investing books.

**Versioning:** each framework is a module with a version string, which is recorded in every decision. Parameter changes create a new version and never edit an old one in place.

---

## Component 3: Macro & Risk Overlay

**Purpose:** catch portfolio-level blind spots and encode cycle awareness. All of it is deterministic code.

**Hard limits (reject the trade if breached):**

- No single position above 5% of the portfolio.
- No sector above 25%.
- Portfolio beta to SPY no higher than 1.2. Beta below 0.6 is reported but doesn't block trades, since the portfolio starts in cash.
- Average pairwise correlation (60-day) of holdings below 0.5.

**Soft adjustments:** these raise the composite-score threshold a new buy must clear.

- **Crowding:** +1 threshold step when a sector is above 20% of the portfolio.
- **Regime:** +1 step when any of the following holds:
  - The 2s10s yield curve is inverted.
  - High-yield spreads have widened by more than 150bp over 3 months.
  - The 3-month change in the effective fed funds rate is more than 50bp.

**Rate expectations:** FRED-based proxies are used in the MVP. The CME FedWatch API is optional later, and only if its cost is justified.

---

## Component 4: Decision Engine

**Purpose:** combine framework math, rubric scores and the risk overlay into actions.

**Pipeline:**

1. Code computes the composite score, the action and the target weight.
   - The rules are explicit, e.g. ACCUMULATE when the screen passes, margin of safety ≥ 25% at the band low, and the composite clears the overlay threshold.
   - Position size uses inverse volatility, capped by the hard limits.
2. Code computes `confidence` as the calibrated probability of beating the primary benchmark over 6 months.
   - Until enough outcomes exist, it is a fixed prior per score bucket.
   - After that, it comes from isotonic regression fitted on resolved predictions.
3. The reasoning LLM receives the computed record and writes thesis bullets. It cannot change any field.
4. **Number-grounding check:**
   - Code extracts every number from the thesis.
   - Each number must match a computed field within rounding.
   - Any mismatch means the thesis is rejected and regenerated once, then the decision is logged with a `thesis_failed` flag.

**Decision record (append-only):**

```json
{
  "decision_id": "2026-11-04-MP-01",
  "as_of": "2026-11-04T21:00:00Z",
  "snapshot_hash": "sha256:…",
  "code_version": "git:abc1234",
  "framework": "graham_defensive@1.0.0",
  "model": {"scorer": "<pinned-model-id>", "writer": "<pinned-model-id>"},
  "ticker": "MP",
  "action": "ACCUMULATE",
  "target_weight": 0.025,
  "confidence": 0.58,
  "computed": {
    "intrinsic_value": 24.10,
    "iv_band": [21.30, 27.40],
    "price": 16.20,
    "margin_of_safety_band_low": 0.24,
    "composite_score": 3.4,
    "overlay_threshold": 3.0,
    "rubric": {"novelty": 3, "materiality": 3, "sentiment": 2}
  },
  "thesis": [
    "Price of 16.20 is 24% below the low end of the intrinsic value band (21.30).",
    "Latest 10-Q scored novelty 3/4 on rare-earth supply agreements.",
    "Overlay threshold 3.0 cleared; materials sector weight below crowding limit."
  ],
  "checks": {"number_grounding": "pass", "schema": "pass"},
  "resolve_on": "2027-05-04"
}
```

*The numbers above are illustrative, not real.*

---

## Component 5: Paper Portfolio

**Execution model:**

- A decision made after the close fills at the next session's open, plus a slippage assumption:
  - 10bp for large caps.
  - 25bp for small caps.
- Dividends and corporate actions are applied from the price feed.

**Metrics:**

- Absolute return.
- Alpha and beta against the primary benchmark.
- Max drawdown.
- Sharpe and Sortino ratios.
- Turnover.
- Tracking error.

All are reported **with confidence intervals**. The minimum track record needed for significance at the current tracking error is shown next to the actual track record length.

---

## Component 6: Evaluation & Learning

**Purpose:** improve parameters only when the evidence supports it.

**Measured continuously:**

- **Ranking quality:** daily rank IC (Spearman) between the composite score and forward 21-day and 126-day excess returns across the whole watchlist. Reported as mean IC, IC t-stat, and a monthly IC series.
- **Calibration:** Brier score and a reliability diagram for `confidence` on resolved predictions.
- **Attribution (deterministic):** each resolved position is tagged by code, for example:
  - `valuation_held`: margin of safety was still positive at resolution.
  - `sector_drag`: the sector ETF return explains most of the miss.
  - `fundamental_break`: the next filing showed EPS or margin deterioration beyond a threshold.
  - `text_signal_wrong`: the rubric direction disagreed with the next quarter's fundamentals.
- **LLM summaries:** the LLM may summarize these tags into a readable monthly report. It does not assign them.

**Parameter updates (gated):**

- **Off until** at least 6 months of resolved 126-day predictions exist.
- **Proposals:** a proposed change, such as down-weighting EPS growth in materials, is a new framework version and must meet all of the following:
  - The supporting pattern appears in at least 30 resolved cases.
  - It improves IC on a chronological holdout, i.e. the most recent 30% of data.
  - It moves each weight by no more than ±25% per update.
- **Adoption:**
  - Adopted versions run in shadow beside the live version for 1 month before replacing it.
  - Each change records the evidence behind it and decays back toward the prior unless it is re-confirmed at the next review.
- **Models:** model versions are pinned. Upgrading the model is treated as a framework version change and restarts shadow evaluation.

---

## Component 7: Scheduler & Monitor

This is plain code, not an agent.

**Jobs:**

- **Daily:** data pull → quality gates → scoring → decisions → fills.
- **Monthly:** evaluation report.
- **Quarterly:** parameter-update review, once it's unlocked.

**Alerts** (email or push) fire on:

- Pipeline failure or a failed quality gate.
- API rate-limit or budget thresholds reaching 80%.
- A daily portfolio move beyond 3σ of trailing 60-day volatility.
- A number-grounding failure rate above 5%.
- A scoring-consistency agreement rate below 80%.

## Technology stack (MVP)

| Layer | Choice |
|---|---|
| Language | Python 3.12, pandas, pydantic schemas, pytest |
| Scheduling | GitHub Actions cron (a ~10-minute daily run fits the free minutes) |
| Storage | Supabase Postgres free tier for everything, including gzip-compressed snapshots (roughly 25 MB a year) |
| Dashboard | Static page rebuilt after each run and published to GitHub Pages |
| Alerts | GitHub issues labelled `amia-alert`, which GitHub emails to the repo owner |
| LLM: text scoring | `claude-haiku-5-5` via the Message Batches API (50% off); rubric system prompt cached |
| LLM: thesis writing | `claude-haiku-5-5` to start. Move to `claude-sonnet-5-5` only if thesis quality fails review; the number-grounding check guards the numbers either way. |
| Data | EDGAR + FRED + Alpaca Basic (prices with split and dividend adjustment, news) + Wikipedia membership, all free |

**Budget (prices checked 2026-11-04):**

| Item | Monthly cost |
|---|---|
| Data (EDGAR, FRED, Alpaca Basic, Wikipedia) | $0 |
| Compute and storage (free tiers) | $0 |
| Text scoring: ~22M input + ~1.1M output tokens on Haiku 5.5 ($0.10 / $0.50 per MTok), batched | ~$1.50 |
| Thesis writing: ~110 decisions/month on Haiku 5.5 | <$0.25 |
| **Expected total** | **≈ $2–5** |
| *Thesis writing on Sonnet 5.5 ($2 / $10 per MTok) instead* | *+~$1.50* |
| *Fallback paid price feed if Alpaca's free terms change: EODHD EOD ($19.99), Massive Stocks Starter ($29) or Tiingo Power ($30)* | *+$20–30* |

**Spend cap:** the scheduler enforces a hard monthly LLM limit of $10 and alerts at 80% of it.

## Build phases

| Phase | Weeks | Deliverable |
|---|---|---|
| 0 | 1–2 | EDGAR/FRED/price ingestion, point-in-time store, snapshot hashing, quality gates |
| 1 | 3–4 | Graham screen and intrinsic value with sensitivity band, unit tests against hand-computed examples |
| 2 | 5–6 | Rubric scoring, decision engine, number-grounding check, decision log |
| 3 | 7–8 | Paper portfolio with fill model, risk overlay, daily scheduler, alerts |
| 4 | Month 3+ | Evaluation reports (IC, calibration, attribution) |
| 5 | Month 7+ | Gated parameter updates and shadow evaluation; quality and cycle frameworks |

## Success criteria

**Month 3: Pipeline & Intelligence**

- At least 99% of scheduled runs complete, or halt cleanly on a failed quality gate.
- Every decision reproduces exactly from its snapshot.
- Filing-alert lead time: the median gap between AMIA's alert and the first matching item in the news feed is positive. It is measured over all scored 8-Ks.

**Month 6: Logic**

- Number-grounding failure rate is below 1%.
- Rubric re-score agreement is at least 85%.
- There are zero framework-rule violations in the decision log, checked by an automated audit.

**Month 12: Signal**

- Mean daily rank IC of the composite score against 126-day excess returns is above 0 with t-stat ≥ 2, or there is a documented conclusion that no edge was found.
- The Brier score beats the base-rate forecast.
- Portfolio alpha is reported with its confidence interval. It is not a pass/fail criterion, because a single year is too short.

**Month 18+: Learning**

- Any adopted parameter change shows improved IC on data collected *after* adoption compared with the version it replaced.
- The rate of repeated error types (by attribution tag) is trending down across at least 3 review cycles.

## Changes from v2.0

| v2.0 | v2.1 | Why |
|---|---|---|
| LLM outputs confidence and allocation | Code computes both; LLM writes prose only | LLM confidence scores aren't calibrated; sizing must be reproducible |
| FMP fundamentals | EDGAR, keyed to filing accepted dates | Avoids restatement leakage into decisions |
| sec-api.io | EDGAR APIs | Free and authoritative |
| CME FedWatch | FRED proxies | Cost; enough for an MVP |
| Month 12 alpha as the test | Rank IC + calibration; alpha shown with its interval | One year of portfolio P&L can't separate skill from luck |
| Monthly permanent "lessons" | Gated, versioned, decaying parameter updates from month 7 | Prevents fitting to noise |
| LLM post-mortem attribution | Deterministic attribution tags | Avoids after-the-fact stories |
| Master AI agent | Scheduler + monitor | No reasoning needed; fewer ways to fail |
| Graham, Buffett and Marks at once | Graham first; quality and cycle frameworks later | One fully tested framework before adding more |
| Unspecified universe | S&P 1500 with membership snapshots | Survivorship and benchmark consistency |
| Claude 3.5 models | Pinned current-generation models | Old models retired; pinning keeps the experiment clean |

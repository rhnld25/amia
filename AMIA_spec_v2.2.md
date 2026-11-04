# Autonomous Market Intelligence Analyst (AMIA) v2.2

*This is v2.0's design as built. A news-driven watchlist feeds a reasoning LLM, which combines framework math and news to make the call inside code-enforced risk limits. Monthly post-mortems turn repeated mistakes into lessons. Progress is measured forward only, on live paper results, and everything runs on free tiers apart from a few dollars of LLM usage.*

## Mission

AMIA is a continuous buy-side research platform. It answers three questions:
- What market developments warrant attention?
- What actions do Graham, Buffett and Marks dictate, given deterministic math and qualitative context?
- How should the frameworks adapt, given past results?

## Architecture

```text
                ┌──────────────────────────────────┐
                │  Orchestrator & Monitor (code)   │
                └────────────────┬─────────────────┘
     ┌─────────────────┬─────────┴───────┬─────────────────┐
     ▼                 ▼                 ▼                 ▼
Data Ingestion    Investment        Macro & Risk      Post-Mortem &
& Discovery       Philosophy        Overlay           Learning Loop
     │                 │                 │                 │
     └─────────────────┼─────────────────┘                 │
                       ▼                                   │
          Decision Matrix (reasoning LLM + code checks)    │
                       ▼                                   │
                Paper Portfolio  ◄─────────────────────────┘
```

## Component 1: Data ingestion & discovery

**Purpose:** find what deserves attention from reliable, machine-readable streams.

| Data | Source | Cost |
|---|---|---|
| News | Alpaca News API (Benzinga), the whole market, every run | Free on Alpaca Basic |
| SEC filings | EDGAR submissions feed. MD&A is extracted from 10-K and 10-Q filings; 8-K text is taken as filed. | Free |
| Fundamentals | EDGAR XBRL `companyfacts`. Each figure becomes usable only once its filing has been made. | Free |
| Prices | Alpaca Basic, full-market (SIP) daily bars, split- and dividend-adjusted | Free |
| Ticker to company and sector | SEC ticker map and submissions profile; SIC codes mapped to 11 sectors | Free |
| Macro | FRED: AAA yield, 10-year Treasury, high-yield spread, 2s10s curve, fed funds, 3-month T-bill | Free |

**Noise filter (in code):**
- An article is scored only if it mentions a high-impact keyword (earnings, guidance, M&A, contracts, FDA, lawsuits, management changes, buybacks, tariffs and so on).
- Roundups tagging more than 5 tickers are dropped, as are non-equity symbols.
- Articles about held or watched names are always kept.
- At most 1,500 articles are scored per run.

**Fast-tier LLM scoring (Qwen 3.7 Flash via OpenRouter, parallel calls):**
- Each article and filing excerpt gets three scores from 0 to 4 against a fixed rubric (`rubrics/text_scoring_v2.md`):
  - **novelty**
  - **sentiment**
  - **strategic relevance**
- Each score set comes with a one-sentence rationale and a verbatim citation.
- 5% of items are scored twice so consistency can be measured.

**Dynamically weighted daily watchlist:**
- Every scored item adds attention to the tickers it names:
  - `attention = Σ decay × (0.25 + relevance/4) × (0.5 + novelty/4)`.
  - Decay has a 3-day half-life over a 7-day window.
- The watchlist is the top 40 names with attention of at least 1.0 that have tradable prices. Each name is weighted by its share of total attention.
- Sentiment shift (last 2 days against the earlier part of the window) is tracked, to catch narratives that are turning.
- Held positions always stay on the list.
- Tickers that don't file with the SEC can still make the list. Their framework inputs are marked unavailable, and the reasoning model is told so.

**Data-quality gates:**
- If more than 25% of watchlist prices are missing, decisions halt.
- A one-day move of 50% or more excludes that stock for the day.
- An AAA yield more than 7 days old raises an alert.

## Component 2: Investment philosophy engine

**Hard-coded math (deterministic, unit-tested):** each framework is an input to the decision. None of them is a gate.

- **Graham** (`frameworks/graham.py`):
  - The 8 defensive criteria: size, current ratio, debt vs net current assets, 10 years of profits, 10 years of dividends, 10-year EPS growth, P/E on 3-year earnings, and P/E × P/B.
  - Intrinsic value `V = EPS × (8.5 + 2g) × 4.4 / Y`, with a sensitivity band of g ± 2pp and Y ± 0.5. Margin of safety is measured at the low end of the band.
- **Buffett** (`frameworks/buffett.py`):
  - 5-year return on equity: the average should be at least 15% and no year below 10%.
  - Years of earnings needed to repay debt: no more than 4.
  - Net margin: positive in every year, with a stability measure (coefficient of variation) of at most 0.35.
  - Equity growth: 5-year compound growth of at least 7%.
  - Owner-earnings yield (operating cash flow minus capex, divided by market cap) above the 10-year Treasury yield.
- **Marks** (`frameworks/marks.py`):
  - Market temperature from three readings:
    - today's high-yield spread against long-run levels (tight at or below 3.5%, wide at or above 6%; FRED only serves about 3 years, so the 3-year percentile is shown as context only),
    - SPY's 12-month return and drawdown,
    - the yield curve.
  - Each stock gets a fear/greed pendulum reading from four inputs:
    - its 52-week position,
    - 12-1 month momentum,
    - drawdown,
    - how upbeat and how heavy its news coverage is.

**Principles for subjective judgments:**
- Three short notes (`principles/graham.md`, `buffett.md`, `marks.md`) are written for this system rather than quoted from the books.
- They guide judgments such as moat durability, supply-chain fragility and cycle position.
- They are small enough to send whole with every decision, from a cached prompt, so a vector database isn't needed.

## Component 3: Macro & risk overlay

**Required confidence:**
- A new position needs the reasoning model's confidence to be at least 0.60.
- The bar rises by 0.05 for each of these:
  - the stock's sector is above 20% of the portfolio (crowding),
  - the 2s10s curve is inverted,
  - high-yield spreads widened more than 150bp in 3 months,
  - fed funds moved more than 50bp in 3 months.

**Hard limits, applied in code after the model decides:**
- No single position above 5% of the portfolio.
- No sector above 25%.
- Unknown-sector names capped at 15% in total.
- Portfolio beta to SPY no higher than 1.2.
- 60-day average pairwise correlation of holdings below 0.5.
- At most 3 new positions a day.

**Sector momentum:** each sector ETF's 3-month return against SPY is computed and included in the dossier.

**Rate expectations:** FRED series stand in for CME FedWatch, which has no free API.

## Component 4: Decision matrix

**Python environment:** computes everything above into a dossier for each stock. The dossier contains:
- price statistics,
- attention,
- the Graham, Buffett and Marks outputs,
- up to 8 scored news and filing items,
- market conditions,
- portfolio state,
- the required confidence,
- active lessons for the sector.

**Reasoning LLM:** Qwen 3.7 Plus via OpenRouter, with reasoning at medium effort and strict JSON-schema output. It reads the dossier, the decision rules and the principles, and answers in v2.0's shape:

```json
{
  "ticker": "MP",
  "action": "ACCUMULATE",
  "target_allocation": 0.025,
  "confidence": 0.66,
  "thesis": [
    "Price sits 30% below the Graham band low; 6 of 8 defensive criteria met.",
    "Novelty 4 and relevance 4 on a long-term rare-earth supply agreement.",
    "Materials weight 4% is far below the crowding level; required confidence 0.60 cleared."
  ],
  "framework_views": {"graham": "…", "buffett": "…", "marks": "…"},
  "key_risks": ["…"],
  "lessons_applied": []
}
```

*The numbers above are illustrative.*

- **Actions:**
  - A held name gets HOLD or EXIT.
  - A name not held gets ACCUMULATE or PASS.
- **Confidence:** the model's probability that the stock beats SPY over the next 126 trading days. It is required for every review, so its calibration can be measured.
- **When names are reviewed:** held names first, then by watchlist rank. A name is re-reviewed after 5 days, after a price move of 5%, or when new scored items arrive. Reviews are capped at 60 a day.

**Code checks on every answer:**
1. **Schema and legality:**
   - The action must fit the position.
   - Confidence must be between 0 and 1.
   - Allocation is clipped to 0.5–5%.
2. **Number grounding:**
   - Every number in the thesis must appear in the dossier, either as given or rounded, or as a percentage of a fraction.
   - A failure is sent back to the model with the reason, once.
   - A buy that still fails is rejected.
   - An exit still goes through, flagged.
3. **Risk overlay:** the required confidence, then the hard limits, then the daily cap. Blocked buys are recorded with the reason.

Every review is stored as an append-only record. Each record points to a content-hashed snapshot, which holds:
- the full dossier,
- the model's answer,
- the point-in-time facts used,
- every prompt, rubric, principle and model version.

## Component 5: Paper portfolio

- Orders made after the close fill at the next open, plus slippage:
  - 10bp for companies of $10B market cap or more.
  - 25bp for the rest.
- Positions are marked daily from total returns, so dividends are included. Cash earns the T-bill rate.
- **Metrics:**
  - Absolute return, maximum drawdown, Sharpe and Sortino, turnover.
  - Beta-adjusted alpha against SPY, with confidence intervals.
  - The track record needed for significance.
- **Benchmarks:**
  - SPY.
  - The 11 sector ETFs (each closed position is compared with its sector ETF).
  - T-bills as the cash proxy.

## Component 6: Post-mortem & learning loop

**Every month:**
- The reasoning model reviews every position closed in the last two months, plus every executed buy whose 6-month window resolved behind SPY.
- Each case includes:
  - the original thesis and dossier,
  - returns against SPY and the sector ETF,
  - framework numbers at entry and at exit,
  - regime changes,
  - news since entry,
  - attribution tags computed in code.
- It names the cause: thesis played out, math failed, sentiment wrong, macro shift, sector drag, bad luck, or other.
- It names the input lens to weight down or up in that situation, and writes a one-sentence lesson.

**Permanent lessons:**
- When at least 3 post-mortems in a year point the same way on the same lens in the same sector, a lesson is created. It stays active until retired by hand.
- Evidence pointing the other way holds a lesson back.
- At least 6 agreeing cases across 2 or more sectors create a market-wide lesson.
- Active lessons go into every future dossier for that sector. The model reports which ones it applied.

**Measured continuously:**
- Daily rank correlation (IC) between each input and later return against SPY, at 21 and 126 trading days. The inputs measured are:
  - the model's confidence,
  - attention,
  - Graham margin of safety,
  - Buffett criteria,
  - Marks pendulum,
  - momentum.
- The Brier score of the model's confidence.
- Grounding failure rate and scoring consistency.

## Component 7: Orchestrator & monitor

Scheduled GitHub Actions jobs:
- **daily:** weekdays at 22:30 UTC.
- **monthly:** post-mortems, lessons and the report.
- **backfill:** run once to seed the watchlist from the last week of news.

Alerts are GitHub issues labelled `amia-alert`, which GitHub emails to the repo owner. They fire on:
- pipeline failures and failed data gates,
- LLM spend at 80% of the $10 monthly cap (the cap itself is hard-enforced),
- a daily portfolio move beyond 3σ,
- grounding failures above 5%,
- scoring consistency below 80%,
- EDGAR errors,
- database size.

## Technology stack & budget

| Layer | Choice | Monthly cost |
|---|---|---|
| Compute | GitHub Actions (public repo) | $0 |
| Storage | Supabase Postgres free tier | $0 |
| Dashboard | GitHub Pages | $0 |
| Data | EDGAR, FRED, Alpaca Basic | $0 |
| LLM, scoring | Qwen 3.7 Flash (up to 1,500 items a day at ~$0.00002 each, measured) | ~$0.70 |
| LLM, decisions | Qwen 3.7 Plus (up to 40 reviews a day at ~$0.006 each, measured) | ~$5 |
| LLM, post-mortems | Qwen 3.7 Plus | <$0.10 |
| **Total** | | **≈ $6, hard cap $10** |

## Success criteria

**Month 3: intelligence**
- Does the watchlist surface shifts in fundamentals or narratives early? This is measured by how often names enter the watchlist before large moves, and by the IC of attention and sentiment shift.

**Month 6: logic**
- Grounding failures below 1%.
- Scoring agreement of at least 85%.
- Zero risk-limit violations in the decision log.

**Month 12: alpha**
- Alpha against SPY, reported with its confidence interval.
- The model's confidence IC above zero, and a Brier score better than the base rate.
- Repeated post-mortem causes trending down once lessons are active.

## Differences from v2.0 that remain

| v2.0 | v2.2 | Why |
|---|---|---|
| FMP / Polygon fundamentals and prices | EDGAR XBRL and Alpaca Basic | Free; figures tied to filing dates |
| sec-api.io | EDGAR APIs | Free and authoritative |
| CME FedWatch | FRED rate series | FedWatch has no free API |
| Vector DB of investing texts | Short principle notes in the repo, sent whole with each decision | Small enough that retrieval isn't needed; avoids quoting copyrighted books |
| Master AI agent | Scheduled code monitor with alerts | Its jobs (rate limits, billing, triggering post-mortems, alerts) need no reasoning |
| DeepSeek / Claude 3.5 models | Qwen 3.7 Flash and Plus via OpenRouter | Claude isn't available in Hong Kong; Qwen is cheap, serves Hong Kong and supports strict JSON output |

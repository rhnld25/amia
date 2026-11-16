# AMIA v3 (draft): universes and strategies

*Draft for review. It extends v2.2 and changes nothing until approved. Open questions are at the end.*

## The idea in one line

**One pipeline, many portfolios.** Data, news scoring and one shared view per instrument are produced once. Each portfolio is a config entry, a **universe** (which instruments it may trade) plus a **strategy** (long, short, hedge). Code turns the shared view into that portfolio's trades within that portfolio's risk limits. Model cost scales with instruments reviewed, not with the number of portfolios.

```text
 news + filings + prices + macro            (one ingest, as today)
              │
              ▼
 instrument catalog ── class · sector · industry · benchmark
              │
              ▼
 triage (Flash) ──► full memo (Plus) ──► shared VIEW per instrument
                                               │
             ┌──────────────┬──────────────────┼──────────────────┐
             ▼              ▼                  ▼                  ▼
        portfolio A    portfolio B        portfolio C   …    portfolio N
     (universe×strategy, own book, own risk limits, own NAV, own lessons)
```

## 1. Universes

### Classification levels

GICS-style levels, coarse to fine:

| Level | Count | Example |
|---|---|---|
| Asset class | 4 | US stock, foreign stock (ADR), ETF, crypto |
| Sector | 11 | Information Technology, Energy |
| **Industry** | ~25 (curated) | Semiconductors, Oil & Gas E&P, Regional Banks |

"Oil stocks" and "chip stocks" are **industries**. GICS has two more levels below sector, industry group and sub-industry. GICS is licensed, so AMIA derives industries from the SEC SIC code it already stores for every company. A short per-ticker override file covers the misfits: AMAT, LRCX and KLAC file under machinery or optics codes but belong in Semiconductors.

### Starter industries and their benchmark ETFs

| Industry | SIC codes | Benchmark |
|---|---|---|
| Semiconductors | 3674 + overrides | SMH |
| Software | 7372 | IGV |
| Internet | 7370 | FDN |
| Oil & Gas E&P | 1311 | XOP |
| Oil services | 1381–1389 | OIH |
| Refining | 2911 | XLE (fallback) |
| Regional banks | 6021–6022 | KRE |
| Insurance | 6311–6411 | KIE |
| Biotech | 2836 | XBI |
| Pharma | 2834 | XLV (fallback) |
| Medical devices | 3841–3845 | IHI |
| Aerospace & defense | 3720–3729, 3812 | ITA |
| Retail | 5200–5999 | XRT |
| Homebuilders | 1531 | ITB |
| Gold & metals mining | 1000–1099 | GDX / XME |
| Airlines | 4512 | JETS |
| *(everything else)* | | its sector ETF, as today |

Every instrument gets its **industry benchmark** for momentum, for case studies ("beat SPY and its benchmark by 5 points") and for hedging. For example, QCOM is measured against SMH, not XLK.

### Asset classes

| Class | Framework inputs | Notes |
|---|---|---|
| **US stocks** | Graham, Buffett, Marks, news | Today's universe. |
| **Foreign stocks (ADRs)** | Same, once 20-F filings are parsed | 20-F filers report in the `ifrs-full` XBRL taxonomy and often in a foreign currency (NVO in DKK), so they need concept mapping and FX conversion. Until then they run on news + Marks, as now. |
| **ETFs** | Marks cycle, momentum against SPY, news on the fund and on its industry's members | No financial statements, so no Graham or Buffett. Also used as hedge instruments. |
| **Crypto** | Marks cycle, momentum, news | Alpaca's crypto bars and news are free. Trades 24/7, so it gets its own daily run at 00:00 UTC, including weekends. |

### Universe definitions

A universe is a filter. Combinations are unions:

```python
UNIVERSES = {
    "us_all":      {"classes": ["us_equity"]},
    "chips":       {"classes": ["us_equity", "adr"], "industries": ["Semiconductors"]},
    "energy":      {"classes": ["us_equity", "adr"], "sectors": ["Energy"]},
    "etfs":        {"classes": ["etf"]},
    "crypto":      {"classes": ["crypto"]},
    "global_mix":  {"classes": ["us_equity", "adr", "etf"]},
}
```

Discovery stays news-driven. The attention ranking now produces **a quota per universe**, so a narrow universe like chips always has candidates even when the overall top 40 is dominated by other names. Each portfolio's watchlist is the top names of its universe.

## 2. Strategies

| Strategy | Long | Short | Hedge |
|---|:-:|:-:|:-:|
| `long_only` | ✓ | | |
| `long_short` | ✓ | ✓ | |
| `long_hedge` | ✓ | | ✓ |
| `long_short_hedge` | ✓ | ✓ | ✓ |

### The shared view

The memo stays one call per instrument. Its answer becomes a **view**, not an action:

```json
{
  "ticker": "QCOM",
  "p_outperform": 0.66,
  "horizon_days": 63,
  "target_allocation": 0.02,
  "thesis": ["…"],
  "bear_case": ["…"],
  "framework_views": {"graham": "…", "buffett": "…", "marks": "…"},
  "key_risks": ["…"],
  "triggers": {"enter_below": 151.0, "stop": 132.0, "take_profit": 190.0},
  "lessons_applied": []
}
```

- `p_outperform` is the probability of beating the instrument's **benchmark** over the horizon. A high value means long, a low value means short. One calibrated number serves both directions.
- `bear_case` is required, so short calls get grounded reasons just as buys do. Number grounding applies to it as well.
- `triggers` are price levels the model sets. Code executes them intraday (see section 3).

### From the view to each portfolio (code)

| Portfolio state | Rule |
|---|---|
| Not held, may go long | Buy if `p ≥ required_long` (0.60 plus regime steps, as today) |
| Not held, may go short | Short if `p ≤ 1 − required_short` (starts at 0.35) |
| Held long | Exit if `p < 0.45`, or on a stop or take-profit trigger |
| Held short | Cover if `p > 0.55`, or on a stop trigger |
| Hedged | Rebalance the hedge after any trade (below) |

### Short rules (code-enforced)

- At most 3% of NAV per short and 30% of NAV in shorts in total.
- Only shares priced above $5 with a market cap above $2B. That's a proxy for easy-to-borrow, since free data has no borrow rates.
- An assumed borrow fee of 0.5% a year accrues daily, and dividends are charged to the short.
- A hard stop covers the short at a 25% adverse move from entry, whatever the view.
- No shorting through earnings week unless the thesis is about the earnings.

### Hedging

The hedge is a paper short of an ETF, in one of two modes, set per portfolio:

- **`market`:** short SPY so that portfolio beta lands at a target (default 0.3). This tests whether stock selection beats the market once market moves are removed.
- **`industry`:** each long is paired with a short of its industry benchmark (long QCOM, short SMH, beta-weighted). This isolates pure stock picking from the industry's swings, which is exactly the QCOM-vs-chip-crash question.

## 3. Speeding up the experiment

| Lever | Today | v3 |
|---|---|---|
| Watchlist | top 40 | quota per universe, ~120 candidates in total |
| Who gets a full memo | everything reviewed, up to 60/day | **Flash triage** reads the dossier first (~$0.0001 each), and Plus writes full memos only for the ~45 with the strongest signal either way |
| New positions | 3/day | 6/day per portfolio |
| Position size | 2.5% typical | 1.5–2% typical, so each book holds 40–60 names |
| Judging horizon | 126 trading days (6 months) | **63** (3 months), checked at 21 and 63 |
| Intraday | watchlist refresh only | **Triggers** from the evening memo run in code every 15 minutes (no model cost). Up to 10 **event reviews** a day: a held name with relevance-4 news gets a fresh Plus memo intraday. |
| Fills | next open | next open (evening decisions), next 15-minute price (triggers), both plus slippage |

The shorter horizon roughly halves the time until post-mortems, lessons, IC and calibration produce real numbers: first resolved calls in about 3 months instead of 6.

## 4. Budget (hard cap stays $10/month)

| Item | Volume | Per month |
|---|---|---:|
| News scoring (Flash) | ~2,000 items/day, including ADR and crypto news | ~$0.90 |
| Triage (Flash) | ~150 dossiers/day | ~$0.35 |
| Full memos (Plus) | ~45/day, plus a few crypto on weekends | ~$6.20 |
| Intraday event reviews (Plus) | ≤10/day, ~4 typical | ~$0.55 |
| Post-mortems and case studies | more, since the horizon is shorter | ~$0.70 |
| **Total** | | **≈ $8.70** |

Portfolios themselves cost $0. When spend nears the cap, code drops work in this order: event reviews, then crypto memos, then the lowest-ranked triage candidates. Held names are never dropped.

## 5. Data model changes

- **`instruments`**: symbol, asset class, sector, industry, benchmark ETF. Replaces the sector column on `symbols`.
- **`portfolios`**: id, name, universe, strategy, hedge mode, parameters, start date.
- **`views`**: the shared memo per instrument per day. This is today's `decisions` without the per-portfolio parts.
- **`portfolio_actions`**: per portfolio per day, the code's translation of the view (BUY, SHORT, EXIT, COVER, HEDGE, PASS, BLOCKED) with reasons.
- **`portfolio_id`** on orders, positions, closed positions, NAV, post-mortems, case studies and lessons.
- The existing portfolio becomes **`core`** (`us_all` × `long_only`) and keeps its full history.

Framework version moves to `quantamental@3.0.0`. Records under 2.0.0 stay as they are.

**As built in phase 1:**
- The catalog is the existing `symbols` table with three new columns (`asset_class`, `industry`, `benchmark`), not a separate `instruments` table.
- Asset class comes from the filer's recent SEC forms: fund forms mean an ETF, 20-F/40-F/6-K without 10-K/10-Q means an ADR. Fund-like names (ETF, FUND), commodity pools (SIC 6221) and a short known list also count as ETFs. Symbols created before v3 are classified gradually, at most 100 per run.
- `positions` and `nav` were rebuilt with `(portfolio_id, …)` keys. `orders`, `closed_positions`, `postmortems` and `case_studies` gained a `portfolio_id` column. Every existing row is `core`, and cash moved to the kv key `cash:core`.
- Lessons stay shared across portfolios, since every portfolio reads the same view.
- Decisions are unchanged: the dossier, the risk sector and the sector-ETF comparisons are exactly as in v2.2. Industry benchmark prices are now fetched daily, so their history accrues before phase 2 starts using them.

## 6. Starter portfolios

| Portfolio | Universe | Strategy |
|---|---|---|
| core | us_all | long_only (today's book, continued) |
| core_ls | us_all | long_short |
| core_hedged | us_all | long_hedge, market mode |
| core_lsh | us_all | long_short_hedge, industry mode |
| chips_ls | chips | long_short_hedge, industry mode |
| energy_ls | energy | long_short_hedge, industry mode |
| global | global_mix | long_short_hedge, market mode |
| crypto | crypto | long_short |

The dashboard gets a comparison chart of all portfolios against SPY, and a tab per portfolio.

## 7. Rollout

1. **Industries and portfolios:** industry mapping and benchmarks, `instruments` and `portfolios`, `portfolio_id` everywhere. `core` keeps running unchanged.
2. **Strategies:** the shared view, short and hedge accounting, short risk limits, and the four US-stock portfolios.
3. **Speed:** universe quotas, Flash triage, the 63-day horizon, intraday triggers and event reviews.
4. **ETFs and ADRs:** ETF dossiers, 20-F (IFRS) parsing with FX, chips, energy and global portfolios.
5. **Crypto:** crypto data, the weekend schedule, the crypto portfolio.

Each phase ships on its own, and forward results start accruing as soon as it lands.

## Open questions

1. **Who decides HOLD and EXIT?** In v2.2 the model picks the action for each stock. With a shared view, the model sets direction, conviction, size and price levels, and code applies each portfolio's permissions. That keeps cost flat but moves the final word to code. The alternative is a short extra model call per held position per portfolio, which costs about +$1–2/month at today's book sizes.
2. **Horizon for `core`:** move it to 63 days with everything else, or keep 126 so its calibration history stays comparable?
3. **Short threshold:** start shorts at `p ≤ 0.35`, or require more conviction than longs (for example 0.30), since losses on shorts have no ceiling?

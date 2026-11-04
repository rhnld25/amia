"""Runtime settings (from environment) and versioned framework parameters.

Parameters that change decisions live in DISCOVERY, FRAMEWORK and DECISION and are versioned:
changing any value means bumping FRAMEWORK_VERSION, never editing in place.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

FRAMEWORK_VERSION = "quantamental@2.0.0"

# Component 1: discovery. The watchlist comes from news traction, not a fixed index.
DISCOVERY = {
    "lookback_days": 7,
    "half_life_days": 3.0,
    "watchlist_size": 40,
    "min_attention": 1.0,  # roughly one novel, relevant item, or several minor ones
    "max_symbols_per_article": 5,  # articles tagging more tickers are market roundups (noise)
    "max_news_per_run": 1500,  # cost bound on scoring
    "keywords": [
        "earnings", "guidance", "outlook", "forecast", "beat", "miss", "revenue", "profit", "loss", "margin",
        "acquire", "acquisition", "merger", "buyout", "takeover", "spin-off", "spinoff", "divest",
        "contract", "deal", "partnership", "agreement", "order", "award",
        "fda", "approval", "approved", "trial", "patent", "launch",
        "lawsuit", "investigation", "probe", "settlement", "subpoena", "fine", "recall", "restatement",
        "bankruptcy", "default", "delisting", "going concern", "downgrade", "upgrade",
        "ceo", "cfo", "resign", "appoint", "layoff", "restructuring",
        "buyback", "repurchase", "dividend", "offering", "dilution",
        "tariff", "sanction", "export", "shortage", "supply chain", "strike",
    ],
}

# Component 2: framework math. Every number here is computed in code, never by the LLM.
FRAMEWORK = {
    # Graham defensive screen
    "min_revenue_usd": 500e6,
    "min_current_ratio": 2.0,
    "earnings_years": 10,
    "dividend_years": 10,
    "min_eps_growth_10y": 0.33,
    "max_pe_3y": 15.0,
    "max_pe_x_pb": 22.5,
    # Intrinsic value: V = EPS * (8.5 + 2g) * 4.4 / Y
    "g_years": 5,
    "g_cap_pct": 15.0,
    "g_band_pp": 2.0,
    "y_band_pct": 0.5,
    # Buffett quality
    "buffett_years": 5,
    "min_roe_avg": 0.15,
    "min_roe_year": 0.10,
    "max_debt_payoff_years": 4.0,
    "max_margin_cv": 0.35,
    "min_equity_cagr": 0.07,
    # Marks cycle
    "hy_lookback_days": 1100,
    # FRED serves only ~3 years of the ICE BofA high-yield spread, too short to tell tight from wide on its own.
    # These levels come from the series' history since 1996 (median near 5%; lows near 2.6-3%, crisis highs 10%+).
    "hy_tight_level": 3.5,
    "hy_wide_level": 6.0,
    "pendulum_high_pos": 0.9,
    "pendulum_hot_sentiment": 3.2,
    "pendulum_cold_sentiment": 1.0,
    "pendulum_hot_momentum": 0.5,
    "pendulum_deep_drawdown": -0.35,
    "text_lookback_days": 30,
}

# Component 4: decision matrix. The reasoning model decides; code enforces these limits around it.
DECISION = {
    "base_confidence": 0.60,  # required P(beat SPY over 6 months) for a new position
    "confidence_step": 0.05,  # added per regime flag or crowded sector
    "max_new_positions_per_day": 3,
    "min_position_weight": 0.005,
    "max_position_weight": 0.05,
    "review_stale_days": 5,  # re-review a name at least this often while it stays on the watchlist
    "review_price_move": 0.05,  # or sooner after a move this large
    "max_reviews_per_day": 60,
    "parallel_reviews": 4,  # reasoning calls in flight at once
    "max_news_in_dossier": 8,
}

RISK = {
    "max_sector_weight": 0.25,
    "crowded_sector_weight": 0.20,
    "max_portfolio_beta": 1.2,
    "min_portfolio_beta_info": 0.6,
    "max_avg_pairwise_corr": 0.5,
    "max_unknown_sector_weight": 0.15,
    "corr_window": 60,
    "beta_window": 252,
    "regime_hy_widening": 1.5,  # percentage points over ~3 months
    "regime_ff_change": 0.5,
    "regime_lookback_obs": 63,
}

MACRO_SERIES = ["DAAA", "AAA", "BAMLH0A0HYM2", "T10Y2Y", "DFF", "DTB3", "DGS10"]

PORTFOLIO = {
    "starting_nav": 100_000.0,
    "slippage_large": 0.0010,
    "slippage_small": 0.0025,
    "large_cap_usd": 10e9,
}

EVALUATION = {
    "horizons": [21, 126],
    "decision_horizon": 126,
    "min_names_for_ic": 8,
}

# Component 6: post-mortem lessons. A lesson needs this many agreeing post-mortems in the window.
# Storage: what news data is kept, so the database stays inside the free tier (see retention.py).
RETENTION = {
    "text_body_days": 45,
    "unwatched_score_days": 30,
}

LEARNING = {
    "lesson_min_cases": 3,
    "lesson_window_days": 365,
}

LENSES = ["llm_conf", "attention", "graham_mos", "buffett_passed", "marks_pendulum", "mom_12_1"]

# OpenRouter price per million tokens (USD), checked 2026-11-04. Used for budget estimates; recorded spend
# uses the cost OpenRouter reports for each call.
MODEL_PRICES = {
    "qwen/qwen3.7-flash": {"input": 0.03, "output": 0.13},
    "qwen/qwen3.7-plus": {"input": 0.32, "output": 1.28},
}

SECTOR_ETFS = {
    "Information Technology": "XLK",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}
BENCHMARKS = ["SPY", *SECTOR_ETFS.values()]


def _git_sha() -> str:
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha[:12]
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///amia_local.db"))
    mock: bool = field(default_factory=lambda: os.environ.get("AMIA_MOCK", "") == "1")
    sec_contact_email: str = field(default_factory=lambda: os.environ.get("SEC_CONTACT_EMAIL", ""))
    fred_api_key: str = field(default_factory=lambda: os.environ.get("FRED_API_KEY", ""))
    alpaca_key_id: str = field(default_factory=lambda: os.environ.get("ALPACA_API_KEY_ID", ""))
    alpaca_secret: str = field(default_factory=lambda: os.environ.get("ALPACA_API_SECRET_KEY", ""))
    openrouter_api_key: str = field(default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", ""))
    scoring_model: str = field(default_factory=lambda: os.environ.get("AMIA_SCORING_MODEL", "qwen/qwen3.7-flash"))
    decision_model: str = field(default_factory=lambda: os.environ.get("AMIA_DECISION_MODEL", "qwen/qwen3.7-plus"))
    llm_monthly_cap_usd: float = field(default_factory=lambda: float(os.environ.get("AMIA_LLM_MONTHLY_CAP_USD", "10")))
    batch_wait_minutes: int = field(default_factory=lambda: int(os.environ.get("AMIA_BATCH_WAIT_MINUTES", "20")))
    github_token: str = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN", ""))
    github_repository: str = field(default_factory=lambda: os.environ.get("GITHUB_REPOSITORY", ""))
    db_size_alert_mb: int = 400
    code_version: str = field(default_factory=_git_sha)

    def require_live(self) -> None:
        missing = [
            name
            for name, value in [
                ("SEC_CONTACT_EMAIL", self.sec_contact_email),
                ("FRED_API_KEY", self.fred_api_key),
                ("ALPACA_API_KEY_ID", self.alpaca_key_id),
                ("ALPACA_API_SECRET_KEY", self.alpaca_secret),
            ]
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required settings: {', '.join(missing)} (or set AMIA_MOCK=1)")

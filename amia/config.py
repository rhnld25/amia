"""Runtime settings (from environment) and versioned framework parameters.

Parameters that change decisions live in FRAMEWORK and are versioned: changing any
value means bumping FRAMEWORK_VERSION, never editing in place.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

FRAMEWORK_VERSION = "graham_defensive@1.0.0"

FRAMEWORK = {
    # Graham defensive screen
    "min_revenue_usd": 500e6,
    "min_current_ratio": 2.0,
    "earnings_years": 10,
    "dividend_years": 10,
    "min_eps_growth_10y": 0.33,
    "max_pe_3y": 15.0,
    "max_pe_x_pb": 22.5,
    "min_criteria_for_pass": 7,  # of 8
    # Intrinsic value: V = EPS * (8.5 + 2g) * 4.4 / Y
    "g_years": 5,
    "g_cap_pct": 15.0,
    "g_band_pp": 2.0,
    "y_band_pct": 0.5,
    # Composite score (0-4 scale)
    "w_value": 0.45,
    "w_quality": 0.35,
    "w_text": 0.20,
    "text_lookback_days": 30,
    # Actions
    "min_mos_band_low": 0.25,
    "base_threshold": 2.75,
    "threshold_step": 0.25,
    "max_new_positions_per_day": 3,
    "max_holding_days": 730,
    # Sizing
    "base_weight": 0.025,
    "max_position_weight": 0.05,
}

RISK = {
    "max_sector_weight": 0.25,
    "crowded_sector_weight": 0.20,
    "max_portfolio_beta": 1.2,
    "min_portfolio_beta_info": 0.6,
    "max_avg_pairwise_corr": 0.5,
    "corr_window": 60,
    "beta_window": 252,
    "regime_hy_widening": 1.5,  # percentage points over ~3 months
    "regime_ff_change": 0.5,
    "regime_lookback_obs": 63,
}

PORTFOLIO = {
    "starting_nav": 100_000.0,
    "slippage_large": 0.0010,
    "slippage_small": 0.0025,
}

EVALUATION = {
    "horizons": [21, 126],
    "decision_horizon": 126,
    "min_resolved_for_calibration": 300,
    "learning_unlock_resolved_days": 126,  # trading days of resolved 126d predictions
}

# Price per million tokens (USD), checked 2026-11-04. Batch requests are billed at 50%.
MODEL_PRICES = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
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
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    scoring_model: str = field(default_factory=lambda: os.environ.get("AMIA_SCORING_MODEL", "claude-haiku-5-5"))
    thesis_model: str = field(default_factory=lambda: os.environ.get("AMIA_THESIS_MODEL", "claude-haiku-5-5"))
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

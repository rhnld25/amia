"""v3 phase 1: instrument classification, the v2.2 -> v3 schema migration and one book per portfolio."""

import os

import pytest

from amia.config import BENCHMARKS, CORE, INDUSTRY_ETFS, PORTFOLIO
from amia.data.sectors import (
    asset_class_for,
    benchmark_for,
    industry_for,
    sector_for_sic,
)
from amia.db import DB
from amia.portfolio import cash, last_nav, metrics, positions, run_day, sync_portfolios


def _db(tmp_path, name="t.db"):
    url = os.environ.get("AMIA_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / name}"
    db = DB(url)
    if not db.is_sqlite:
        db.execute("DROP SCHEMA public CASCADE")
        db.execute("CREATE SCHEMA public")
    return db


@pytest.mark.parametrize(
    "symbol,name,sic,forms,has_cik,asset_class,industry,benchmark",
    [
        ("QCOM", "QUALCOMM INC", "3674", {"10-K", "10-Q"}, True, "us_equity", "Semiconductors", "SMH"),
        ("LRCX", "LAM RESEARCH", "3559", {"10-K"}, True, "us_equity", "Semiconductors", "SMH"),  # override
        ("XOM", "EXXON MOBIL", "2911", {"10-K"}, True, "us_equity", "Refining", "XLE"),
        ("DVN", "DEVON ENERGY", "1311", {"10-K"}, True, "us_equity", "Oil & Gas E&P", "XOP"),
        ("AMZN", "AMAZON COM INC", "5961", {"10-K"}, True, "us_equity", "Internet", "FDN"),
        ("WMT", "Walmart Inc.", "5331", {"10-K"}, True, "us_equity", "Retail", "XRT"),
        ("TRMK", "TRUSTMARK CORP", "6022", {"10-K"}, True, "us_equity", "Banks", "KBE"),
        ("LDOS", "Leidos", "7373", {"10-K"}, True, "us_equity", None, "XLK"),  # no industry: sector ETF
        ("NVO", "NOVO NORDISK A S", "2834", {"20-F", "6-K"}, True, "adr", "Pharma", "PPH"),
        ("SPY", "SPDR S&P 500 ETF TRUST", None, set(), True, "etf", None, "SPY"),
        ("USO", "United States Oil Fund, LP", "6221", {"10-K"}, True, "etf", None, "SPY"),  # files like a company
        ("ARKK", "ARK ETF Trust", None, {"N-CEN", "NPORT-P"}, True, "etf", None, "SPY"),
        ("BTC/USD", None, None, None, False, "crypto", None, "SPY"),
        ("XYZQ", None, None, None, False, "other", None, "SPY"),
    ],
)
def test_classification(symbol, name, sic, forms, has_cik, asset_class, industry, benchmark):
    ac = asset_class_for(symbol, name, sic, forms, has_cik)
    ind = industry_for(symbol, sic) if ac in ("us_equity", "adr") else None
    assert (ac, ind, benchmark_for(ac, sector_for_sic(sic), ind)) == (asset_class, industry, benchmark)


def test_industry_benchmarks_are_fetched():
    assert set(INDUSTRY_ETFS.values()) <= set(BENCHMARKS)
    assert len(BENCHMARKS) == len(set(BENCHMARKS))


# v2.2's tables as deployed, before portfolio_id existed
_V22 = """
CREATE TABLE symbols (symbol TEXT PRIMARY KEY, cik TEXT, name TEXT, sector TEXT, sic TEXT, first_seen TEXT,
    filings_checked TEXT, no_price_since TEXT);
CREATE TABLE orders (order_id TEXT PRIMARY KEY, decision_id TEXT, symbol TEXT, side TEXT, target_weight REAL,
    created_date TEXT, status TEXT, fill_date TEXT, fill_price REAL, amount REAL);
CREATE TABLE positions (symbol TEXT PRIMARY KEY, sector TEXT, opened_date TEXT, entry_price REAL, value REAL,
    cost REAL, decision_id TEXT, last_close REAL);
CREATE TABLE closed_positions (id TEXT PRIMARY KEY, symbol TEXT, sector TEXT, opened_date TEXT, closed_date TEXT,
    entry_price REAL, exit_price REAL, ret REAL, excess_ret REAL, sector_excess_ret REAL,
    decision_id TEXT, exit_decision_id TEXT, tags TEXT);
CREATE TABLE nav (date TEXT PRIMARY KEY, nav REAL, cash REAL, invested REAL, ret REAL,
    spy_ret REAL, rf_ret REAL, turnover REAL);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)
"""


def test_v22_database_migrates_to_core(tmp_path):
    db = _db(tmp_path)
    for stmt in _V22.replace("REAL", "REAL" if db.is_sqlite else "DOUBLE PRECISION").split(";"):
        db.execute(stmt)
    db.execute("INSERT INTO symbols (symbol, cik, name, sector, sic) VALUES ('QCOM', '0000804328', 'QUALCOMM', 'Information Technology', '3674')")
    db.execute("INSERT INTO positions VALUES ('QCOM', 'Information Technology', '2026-11-05', 150.21, 2900, 2500, '2026-11-04-QCOM', 172.47)")
    db.execute("INSERT INTO nav VALUES ('2026-11-03', 100000, 100000, 0, 0, 0, 0.0002, 0)")
    db.execute("INSERT INTO nav VALUES ('2026-11-04', 100100, 97600, 2500, 0.001, 0.002, 0.0002, 0.025)")
    db.execute("INSERT INTO orders VALUES ('2026-11-04-QCOM', '2026-11-04-QCOM', 'QCOM', 'BUY', 0.025, '2026-11-04', 'filled', '2026-11-05', 150.21, 2500)")
    db.execute("INSERT INTO closed_positions (id, symbol, closed_date) VALUES ('GOOG-2026-11-05-2026-11-09', 'GOOG', '2026-11-09')")
    db.execute("INSERT INTO kv VALUES ('cash', '97600.5')")

    db.init_schema()
    db.init_schema()  # a second start is a no-op

    assert positions(db)["QCOM"]["value"] == 2900 and positions(db)["QCOM"]["portfolio_id"] == CORE
    assert last_nav(db) == 100100 and metrics(db)["days"] == 1
    assert cash(db) == 97600.5
    assert db.scalar("SELECT portfolio_id FROM orders") == CORE
    assert db.scalar("SELECT portfolio_id FROM closed_positions") == CORE
    assert db.query("SELECT asset_class, industry, benchmark FROM symbols") == [{"asset_class": None, "industry": None, "benchmark": None}]
    # the new keys let another book hold the same symbol on the same dates
    db.upsert("positions", [{"portfolio_id": "other", "symbol": "QCOM", "value": 1.0}], ["portfolio_id", "symbol"])
    db.upsert("nav", [{"portfolio_id": "other", "date": "2026-11-04", "nav": 1.0}], ["portfolio_id", "date"])
    assert positions(db)["QCOM"]["value"] == 2900 and last_nav(db) == 100100


def test_portfolios_keep_separate_books(tmp_path):
    db = _db(tmp_path)
    db.init_schema()
    sync_portfolios(db)
    assert db.scalar("SELECT strategy FROM portfolios WHERE portfolio_id = :p", {"p": CORE}) == "long_only"

    def order(pid, sym, side, w, day):
        return {"order_id": f"{pid}:{day}-{sym}", "portfolio_id": pid, "decision_id": f"{day}-{sym}", "symbol": sym, "side": side,
                "target_weight": w, "created_date": day, "status": "pending", "fill_date": None, "fill_price": None, "amount": None}

    db.upsert("orders", [order(CORE, "AAA", "BUY", 0.05, "2026-01-01"), order("test", "BBB", "BUY", 0.10, "2026-01-01")], ["order_id"])
    bars = {"AAA": {"open": 10.0, "close": 11.0, "ret": 0.1}, "BBB": {"open": 20.0, "close": 18.0, "ret": -0.1}}
    for pid in (CORE, "test"):
        run_day(db, "2026-01-02", bars, {}, {"AAA": "large", "BBB": "large"}, {}, 0.0, 0.0, pid=pid)

    assert set(positions(db, CORE)) == {"AAA"} and set(positions(db, "test")) == {"BBB"}
    assert cash(db, CORE) == pytest.approx(PORTFOLIO["starting_nav"] * 0.95)
    assert cash(db, "test") == pytest.approx(PORTFOLIO["starting_nav"] * 0.90)
    assert last_nav(db, CORE) > PORTFOLIO["starting_nav"] > last_nav(db, "test")

    # selling in one book leaves the other alone
    db.upsert("orders", [order("test", "BBB", "SELL", 0.0, "2026-01-02")], ["order_id"])
    for pid in (CORE, "test"):
        run_day(db, "2026-01-05", {"AAA": {"open": 11.0, "close": 11.0, "ret": 0.0}, "BBB": {"open": 18.0, "close": 18.0, "ret": 0.0}},
                {"AAA": 11.0, "BBB": 18.0}, {"AAA": "large", "BBB": "large"}, {}, 0.0, 0.0, pid=pid)
    assert set(positions(db, CORE)) == {"AAA"} and positions(db, "test") == {}
    assert db.scalar("SELECT portfolio_id FROM closed_positions") == "test"
    assert db.scalar("SELECT id FROM closed_positions").startswith("test:")

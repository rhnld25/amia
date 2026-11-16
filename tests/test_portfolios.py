"""v3 phase 1: instrument classification, the v2.2 -> v3 schema migration and one book per portfolio."""

import os

import pytest

from amia.config import BENCHMARKS, CORE, HEDGE, INDUSTRY_ETFS, PORTFOLIO, SHORT
from amia.data.sectors import (
    asset_class_for,
    benchmark_for,
    industry_for,
    sector_for_sic,
)
from amia.db import DB
from amia.portfolio import cash, last_nav, metrics, positions, run_day, sync_portfolios
from amia.risk import PortfolioState, check_short, hedge_targets


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


def _order(pid, sym, side, w, day):
    return {"order_id": f"{pid}:{day}-{sym}-{side}", "portfolio_id": pid, "decision_id": f"{day}-{sym}", "symbol": sym, "side": side,
            "target_weight": w, "created_date": day, "status": "pending", "fill_date": None, "fill_price": None, "amount": None}


def _bar(o, c, r):
    return {"open": o, "close": c, "ret": r}


def test_short_accounting(tmp_path):
    db = _db(tmp_path)
    db.init_schema()
    start = PORTFOLIO["starting_nav"]
    db.upsert("orders", [_order("ls", "XYZ", "SHORT", 0.02, "2026-01-01")], ["order_id"])
    run_day(db, "2026-01-02", {"XYZ": _bar(100.0, 90.0, -0.1)}, {}, {"XYZ": "large"}, {}, 0.0, 0.0, pid="ls")
    p = positions(db, "ls")["XYZ"]
    assert p["side"] == "short" and p["kind"] == "stock"
    fill = 100.0 * (1 - 0.001)
    assert p["entry_price"] == pytest.approx(fill)
    assert p["value"] == pytest.approx(2000 * 90.0 / fill)  # the liability shrinks as the price falls
    fee = p["value"] * SHORT["borrow_fee_annual"] / 252
    assert cash(db, "ls") == pytest.approx(start + 2000 - fee)  # proceeds in cash, less a day's borrow fee
    assert last_nav(db, "ls") == pytest.approx(start + 2000 - fee - p["value"])  # a gain: the stock fell
    assert last_nav(db, "ls") > start

    # a rise the next day costs the short, and covering at the open closes it with the trade's own return
    run_day(db, "2026-01-05", {"XYZ": _bar(90.0, 99.0, 0.1)}, {"XYZ": 90.0}, {"XYZ": "large"}, {}, 0.0, 0.0, pid="ls")
    assert positions(db, "ls")["XYZ"]["value"] == pytest.approx(p["value"] * 1.1)
    db.upsert("orders", [_order("ls", "XYZ", "COVER", 0.0, "2026-01-05")], ["order_id"])
    run_day(db, "2026-01-06", {"XYZ": _bar(99.0, 99.0, 0.0)}, {"XYZ": 99.0}, {"XYZ": "large"}, {}, 0.0, 0.0, pid="ls")
    assert positions(db, "ls") == {}
    c = db.query("SELECT * FROM closed_positions WHERE portfolio_id = 'ls'")[0]
    assert c["side"] == "short" and c["exit_price"] == pytest.approx(99.0 * 1.001)
    assert c["ret"] == pytest.approx(1 - (p["value"] * 1.1 * 99.0 * 1.001 / 99.0) / 2000)  # sold ~99.9, bought back ~99.1
    for r in db.query("SELECT nav, cash, invested FROM nav WHERE portfolio_id = 'ls'"):
        assert r["nav"] == pytest.approx(r["cash"] + r["invested"])


def test_hedge_opens_resizes_and_closes(tmp_path):
    db = _db(tmp_path)
    db.init_schema()
    db.upsert("orders", [_order("h", "SPY", "HEDGE", 0.10, "2026-01-01")], ["order_id"])
    run_day(db, "2026-01-02", {"SPY": _bar(500.0, 505.0, 0.01)}, {}, {"SPY": "large"}, {}, 0.0, 0.0, pid="h")
    p = positions(db, "h")["SPY"]
    assert (p["side"], p["kind"]) == ("short", "hedge") and p["value"] == pytest.approx(10_000 * 505 / 500)
    nav1 = last_nav(db, "h")
    db.upsert("orders", [_order("h", "SPY", "HEDGE", 0.05, "2026-01-02")], ["order_id"])
    run_day(db, "2026-01-05", {"SPY": _bar(505.0, 505.0, 0.0)}, {"SPY": 505.0}, {"SPY": "large"}, {}, 0.0, 0.0, pid="h")
    assert positions(db, "h")["SPY"]["value"] == pytest.approx(0.05 * nav1)  # resized to 5% of NAV
    db.upsert("orders", [_order("h", "SPY", "HEDGE", 0.0, "2026-01-05")], ["order_id"])
    run_day(db, "2026-01-06", {"SPY": _bar(505.0, 505.0, 0.0)}, {"SPY": 505.0}, {"SPY": "large"}, {}, 0.0, 0.0, pid="h")
    assert positions(db, "h") == {}
    assert db.scalar("SELECT kind FROM closed_positions WHERE portfolio_id = 'h'") == "hedge"
    for r in db.query("SELECT nav, cash, invested FROM nav WHERE portfolio_id = 'h'"):
        assert r["nav"] == pytest.approx(r["cash"] + r["invested"])


def test_short_limits_and_hedge_targets():
    state = PortfolioState(nav=100_000, weights={"A": 0.05, "B": 0.05}, sectors={"A": "Energy", "B": "Information Technology", "S": "Energy"},
                           betas={"A": 1.2, "B": 0.8, "S": 1.0}, shorts={"S": 0.03})
    ok, _ = check_short("X", "Energy", 0.02, 50.0, 5e9, state)
    assert ok
    assert not check_short("X", "Energy", 0.04, 50.0, 5e9, state)[0]  # above the 3% per-short cap
    assert not check_short("X", "Energy", 0.02, 4.0, 5e9, state)[0]  # penny stock
    assert not check_short("X", "Energy", 0.02, 50.0, None, state)[0]  # unknown size: may be hard to borrow
    full = PortfolioState(nav=1, weights={}, sectors={}, shorts={f"S{i}": 0.03 for i in range(10)})
    assert "total short limit" in check_short("X", "Energy", 0.02, 50.0, 5e9, full)[1]["failures"]

    # market mode: short SPY down to the target beta; industry mode: offset each benchmark's net long weight
    assert hedge_targets(state, "market", {})["SPY"] == 0.0  # book beta 0.07 is already under the target
    big = PortfolioState(nav=1, weights={"A": 0.4, "B": 0.3}, sectors={}, betas={"A": 1.2, "B": 0.8})
    assert hedge_targets(big, "market", {})["SPY"] == pytest.approx(0.4 * 1.2 + 0.3 * 0.8 - HEDGE["market_target_beta"])
    assert hedge_targets(state, "industry", {"A": "XLE", "S": "XLE", "B": "SMH"}) == {"XLE": 0.02, "SMH": 0.05}
    assert hedge_targets(state, None, {}) == {}

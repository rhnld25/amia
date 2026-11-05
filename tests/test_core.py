import pytest

from amia import stats
from amia.data.edgar import extract_facts, extract_section
from amia.data.sectors import sector_for_sic
from amia.db import DB
from amia.decision import extract_numbers, fake_decision, grounding_check, validate_decision
from amia.discovery import attention_table, build_watchlist, keep_news
from amia.frameworks import buffett, graham, marks
from amia.learning import lessons_for, update_lessons
from amia.retention import prune


def _facts(eps: list[float], price_div=0.5, ca=300.0, cl=100.0, ltd=150.0, equity=1000.0, shares=100.0, ni_scale=100.0):
    rows = []
    for i, e in enumerate(eps):
        year = 2015 + i
        filed = f"{year + 1}-02-15"
        accn = f"a{year}"
        for concept, value, start in [
            ("EarningsPerShareDiluted", e, f"{year}-01-01"),
            ("Revenues", 1e9, f"{year}-01-01"),
            ("NetIncomeLoss", e * ni_scale, f"{year}-01-01"),
            ("NetCashProvidedByUsedInOperatingActivities", e * ni_scale * 1.2, f"{year}-01-01"),
            ("PaymentsToAcquirePropertyPlantAndEquipment", 10.0, f"{year}-01-01"),
            ("CommonStockDividendsPerShareDeclared", price_div, f"{year}-01-01"),
        ]:
            rows.append({"concept": concept, "period_start": start, "period_end": f"{year}-12-31", "value": value, "filed": filed, "accn": accn})
        for concept, value in [("AssetsCurrent", ca), ("LiabilitiesCurrent", cl), ("LongTermDebtNoncurrent", ltd), ("StockholdersEquity", equity), ("EntityCommonStockSharesOutstanding", shares)]:
            rows.append({"concept": concept, "period_start": None, "period_end": f"{year}-12-31", "value": value, "filed": filed, "accn": accn})
    return rows


def test_graham_value_formula():
    assert graham.graham_value(2.0, 5.0, 4.4) == pytest.approx(2.0 * (8.5 + 10))


def test_graham_full_pass_and_band():
    eps = [1.0, 1.1, 1.2, 1.25, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 2.0]
    pit = graham.point_in_time(_facts(eps), "2026-06-01")
    res = graham.evaluate(pit, price=20.0, aaa_yield=5.0)
    assert res.criteria_passed == 8, res.criteria
    assert res.iv_low < res.iv_mid < res.iv_high
    # g = 5y CAGR of 1.4 -> 2.0, in percent, capped at 15
    assert res.g_pct == pytest.approx(((2.0 / 1.4) ** 0.2 - 1) * 100)
    assert res.mos_band_low == pytest.approx(1 - 20.0 / res.iv_low)


def test_point_in_time_ignores_future_filings_and_uses_restatement_once_known():
    rows = _facts([1.0] * 11)
    rows.append({"concept": "EarningsPerShareDiluted", "period_start": "2025-01-01", "period_end": "2025-12-31", "value": 0.5, "filed": "2026-09-01", "accn": "restated"})
    before = graham.point_in_time(rows, "2026-06-01")["EarningsPerShareDiluted"]["2025-12-31"]
    after = graham.point_in_time(rows, "2026-10-01")["EarningsPerShareDiluted"]["2025-12-31"]
    assert before == 1.0 and after == 0.5
    assert "2025-12-31" not in graham.point_in_time(rows, "2026-01-01").get("EarningsPerShareDiluted", {})


def test_graham_fails_on_losses_and_debt():
    eps = [1.0, -0.5, 1.2, 1.25, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 2.0]
    res = graham.evaluate(graham.point_in_time(_facts(eps, ltd=1000.0), "2026-06-01"), 20.0, 5.0)
    assert not res.criteria["earnings_stability"]
    assert not res.criteria["debt_vs_nca"]


def test_buffett_quality():
    eps = [1.0, 1.1, 1.2, 1.25, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 2.0]
    # equity 1000 and net income 100-200: ROE 10-20%, no debt payoff problem
    pit = graham.point_in_time(_facts(eps, ltd=150.0, equity=800.0), "2026-06-01")
    res = buffett.evaluate(pit, price=20.0, treasury_10y=4.0)
    assert res.roe_avg == pytest.approx((1.5 + 1.6 + 1.7 + 1.8 + 2.0) * 100 / 800 / 5)
    assert res.criteria["consistent_roe"] and res.criteria["low_debt"] and res.criteria["durable_margins"]
    assert not res.criteria["equity_growth"]  # flat equity
    assert res.market_cap == pytest.approx(20.0 * 100)
    assert res.owner_earnings == pytest.approx(2.0 * 100 * 1.2 - 10.0)
    assert res.criteria["owner_earnings_yield"]  # 230 / 2000 = 11.5% vs 4%


def test_marks_price_stats_and_pendulum():
    rets = [0.002] * 300
    ps = marks.price_stats(rets)
    assert ps["pos_52w"] == pytest.approx(1.0) and ps["drawdown_52w"] == pytest.approx(0.0)
    assert ps["mom_12_1"] == pytest.approx(1.002 ** 231 - 1)
    hot = marks.stock_pendulum(ps, avg_sentiment=3.5, attention_top_quartile=True)
    assert hot["label"] == "greed" and hot["score"] == 2
    crash = marks.price_stats([0.0] * 200 + [-0.01] * 60)
    cold = marks.stock_pendulum(crash, avg_sentiment=0.5, attention_top_quartile=False)
    assert cold["label"] == "fear"
    market = marks.market_cycle({"BAMLH0A0HYM2": [(str(i), 5.0 - i / 100) for i in range(300)]}, rets)
    assert market["temperature"] == "hot" and market["hy_percentile"] == pytest.approx(1 / 300)
    # a spread high for the last 3 years but tight by long-run standards is not fear
    recent_high = marks.market_cycle({"BAMLH0A0HYM2": [(str(i), 2.6 + i / 400) for i in range(300)] + [("x", 3.43)]}, rets)
    assert recent_high["hy_percentile"] > 0.8 and recent_high["hy_long_run_band"] == "tight" and recent_high["temperature"] != "cold"
    crisis = marks.market_cycle({"BAMLH0A0HYM2": [(str(i), 4.0 + i / 100) for i in range(300)]}, rets)
    assert crisis["hy_long_run_band"] == "wide" and crisis["temperature_score"] <= 0


def test_sic_to_sector():
    assert sector_for_sic("2834") == "Health Care"
    assert sector_for_sic(7372) == "Information Technology"
    assert sector_for_sic("7370") == "Communication Services"
    assert sector_for_sic("1311") == "Energy"
    assert sector_for_sic(None) == "Unknown" and sector_for_sic("9999") == "Unknown"


def test_news_noise_filter():
    item = {"symbols": ["ABC"], "headline": "ABC wins Pentagon contract", "summary": ""}
    assert keep_news(item, set()) == ["ABC"]
    assert keep_news({**item, "headline": "ABC shares drift in quiet trade"}, set()) == []  # no high-impact keyword
    assert keep_news({**item, "headline": "ABC shares drift in quiet trade"}, {"ABC"}) == ["ABC"]  # held names always kept
    roundup = {"symbols": list("ABCDEFG"), "headline": "Earnings movers", "summary": ""}
    assert keep_news(roundup, set()) == []
    assert keep_news({**item, "symbols": ["BTCUSD", "X1"]}, set()) == []


def test_attention_decays_and_watchlist_keeps_held():
    items = [
        {"symbols": "|NEW|", "published_at": "2026-06-01T14:00:00Z", "novelty": 4, "sentiment": 4, "relevance": 4},
        {"symbols": "|OLD|", "published_at": "2026-05-26T14:00:00Z", "novelty": 4, "sentiment": 0, "relevance": 4},
        {"symbols": "|NEW|OLD|", "published_at": "2026-06-01T10:00:00Z", "novelty": 1, "sentiment": 2, "relevance": 1},
    ]
    t = attention_table(items, "2026-06-01")
    assert t["NEW"]["attention"] > t["OLD"]["attention"]
    assert t["NEW"]["items"] == 2 and t["NEW"]["avg_sentiment"] > 3
    wl = build_watchlist(t, held={"HELD"})
    syms = [w["symbol"] for w in wl]
    assert syms[0] == "NEW" and "HELD" in syms and "OLD" not in syms  # OLD decayed below the floor
    assert sum(w["weight"] for w in wl) == pytest.approx(1.0)
    assert build_watchlist(t, set(), eligible={"OLD"}) == []



def test_share_classes_take_one_watchlist_slot():
    row = lambda a: {"attention": a, "items": 5, "avg_sentiment": 2.0, "sentiment_shift": 0.0, "max_relevance": 3}
    t = {"GOOG": row(3.3), "GOOGL": row(3.1), "AAPL": row(7.0)}
    cik = {"GOOG": "0001652044", "GOOGL": "0001652044", "AAPL": "0000320193"}
    assert [w["symbol"] for w in build_watchlist(t, set(), company=cik)] == ["AAPL", "GOOG"]
    # the class already held keeps the slot, even with less attention
    assert [w["symbol"] for w in build_watchlist(t, {"GOOGL"}, company=cik)] == ["AAPL", "GOOGL"]

def _dossier(held=None):
    return {
        "ticker": "XYZ", "sector": "Energy", "held": held,
        "graham": {"mos_band_low": 0.3125, "criteria_passed": 6, "criteria_total": 8},
        "buffett": {"criteria_passed": 4, "criteria_total": 5},
        "attention": {"attention": 3.217, "avg_sentiment": 3.4},
        "marks": {"stock": {"score": -1, "label": "fear"}, "market": {"temperature": "neutral"}},
        "risk_overlay": {"required_confidence": 0.6}, "lessons": [],
    }


def test_fake_decision_is_grounded_and_legal():
    for held in (None, {"weight": 0.03}):
        ds = _dossier(held)
        d, problems = validate_decision(fake_decision(ds), "XYZ", held is not None)
        assert not problems, problems
        ok, bad = grounding_check(d["thesis"], [ds, d["confidence"], d["target_allocation"]])
        assert ok, bad
    assert fake_decision(_dossier())["action"] == "ACCUMULATE"


def test_validate_decision_rules():
    base = {"action": "ACCUMULATE", "confidence": 0.7, "target_allocation": 9, "thesis": ["a"]}
    d, problems = validate_decision(base, "XYZ", held=False)
    assert not problems and d["target_allocation"] == 0.05  # 9 read as 9%, clipped to the 5% cap
    _, problems = validate_decision(base, "XYZ", held=True)
    assert problems  # a held name can only be held or exited
    _, problems = validate_decision({**base, "confidence": 1.4}, "XYZ", held=False)
    assert problems
    d, _ = validate_decision({**base, "action": "PASS"}, "XYZ", held=False)
    assert d["target_allocation"] == 0.0


def test_lessons_need_agreement(tmp_path):
    db = DB(f"sqlite:///{tmp_path / 'l.db'}")
    db.init_schema()

    def pm(i, sector="Energy", lens="news_sentiment", adj="weight_down"):
        return {"id": f"p{i}", "month": "2026-05", "case_type": "closed_position", "symbol": "X", "sector": sector, "decision_id": "d",
                "cause": "sentiment_wrong", "lens": lens, "adjustment": adj, "explanation": "", "lesson": f"lesson {i}",
                "excess_ret": -0.1, "record": "{}", "created_at": "2026-05-15T00:00:00Z"}

    db.upsert("postmortems", [pm(1), pm(2)], ["id"])
    assert update_lessons(db, "2026-06-01") == []
    db.upsert("postmortems", [pm(3)], ["id"])
    new = update_lessons(db, "2026-06-01")
    assert len(new) == 1 and new[0]["guidance"] == "lesson 3"
    assert update_lessons(db, "2026-06-01") == []  # already active
    assert lessons_for(db, "Energy") and not lessons_for(db, "Utilities")
    # contradicting evidence holds a lesson back
    db.upsert("postmortems", [pm(i, sector="Utilities") for i in (4, 5, 6)] + [pm(i, sector="Utilities", adj="weight_up") for i in (7, 8, 9)], ["id"])
    assert not [x for x in update_lessons(db, "2026-06-01") if x["sector"] == "Utilities"]


def test_retention_keeps_what_the_track_record_needs(tmp_path):
    db = DB(f"sqlite:///{tmp_path / 'r.db'}")
    db.init_schema()
    db.upsert("watchlist", [{"date": "2026-01-05", "symbol": "WAT", "rank": 1}], ["date", "symbol"])

    def item(i, day, status="scored"):
        return {"item_id": i, "item_type": "news", "symbols": "|X|", "published_at": f"{day}T12:00:00Z", "body": b"x", "status": status}

    def score(i, day, syms, p="primary"):
        return {"item_id": i, "score_pass": p, "symbols": syms, "published_at": f"{day}T12:00:00Z", "novelty": 2, "sentiment": 2, "relevance": 2}

    db.upsert("text_items", [item("old", "2026-01-02"), item("new", "2026-03-01"), item("stuck", "2026-01-02", "pending")], ["item_id"])
    db.upsert("text_scores", [score("a", "2026-01-02", "|ZZZ|"), score("a", "2026-01-02", "|ZZZ|", "recheck"), score("b", "2026-01-02", "|ZZZ|WAT|"),
                              score("c", "2026-02-20", "|ZZZ|")], ["item_id", "score_pass"])

    def spend(i, ts, usd):
        return {"id": i, "ts": ts, "model": "m", "purpose": "scoring", "input_tokens": 10, "output_tokens": 1, "cache_read": 0, "cache_write": 0, "batch": 0, "usd": usd}

    db.upsert("llm_spend", [spend("s1", "2026-01-10T01:00:00", 0.01), spend("s2", "2026-01-10T02:00:00", 0.02), spend("s3", "2026-02-10T01:00:00", 0.05)], ["id"])

    out = prune(db, "2026-03-05")
    assert {r["item_id"] for r in db.query("SELECT item_id FROM text_items")} == {"new", "stuck"}  # unscored items wait
    kept = {(r["item_id"], r["score_pass"]) for r in db.query("SELECT item_id, score_pass FROM text_scores")}
    assert kept == {("b", "primary"), ("c", "primary")}  # watched names kept forever, recent ones for now
    rows = db.query("SELECT id, usd, batch FROM llm_spend ORDER BY id")
    assert [(r["id"], round(r["usd"], 4), r["batch"]) for r in rows] == [("day:2026-01-10:m:scoring", 0.03, 2), ("s3", 0.05, 0)]
    assert out == {"text_items": 1, "text_scores": 2, "llm_spend": 2}
    assert prune(db, "2026-03-05") == {"text_items": 0, "text_scores": 0, "llm_spend": 0}


def test_number_grounding():
    facts = {"price": 16.2, "iv_low": 21.3, "mos_band_low": 0.2394, "criteria_passed": 7, "criteria_total": 8}
    ok, bad = grounding_check(["Price $16.20 is 24% below 21.30; passes 7 of 8 criteria on a 0-4 scale per the 10-K."], facts)
    assert ok, bad
    ok, bad = grounding_check(["Price 16.20 implies a 31% margin of safety."], facts)
    assert not ok and bad == ["31%"]
    ok, _ = grounding_check(["Headline says revenue hit $4.2B."], {"news": [{"text": "Revenue rose to $4.2B"}]})
    assert ok  # numbers quoted from the dossier's news text are allowed


def test_extract_numbers_ignores_forms():
    nums = [v for _, v, _ in extract_numbers("QAB filed a 10-Q; EPS rose to 1.25 (up 3%).")]
    assert nums == [1.25, 3.0]


def test_stats():
    assert stats.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert stats.max_drawdown([100, 120, 90, 130]) == pytest.approx(-0.25)
    a, b, _, _ = stats.ols([0.01, -0.02, 0.03, 0.0, 0.01], [0.02, -0.04, 0.06, 0.0, 0.02])
    assert b == pytest.approx(2.0) and a == pytest.approx(0.0, abs=1e-12)
    steps = stats.isotonic_fit([1, 2, 3], [1, 0, 1])
    assert [v for _, v in steps] == [0.5, 1.0]


def test_extract_facts_filters():
    payload = {
        "facts": {
            "us-gaap": {
                "EarningsPerShareDiluted": {
                    "units": {
                        "USD/shares": [
                            {"start": "2024-01-01", "end": "2024-12-31", "val": 2.0, "accn": "a", "form": "10-K", "filed": "2025-02-01"},
                            {"start": "2024-10-01", "end": "2024-12-31", "val": 0.5, "accn": "a", "form": "10-K", "filed": "2025-02-01"},
                            {"start": "2024-01-01", "end": "2024-12-31", "val": 2.0, "accn": "b", "form": "10-K", "filed": "2026-02-01"},
                            {"start": "2024-01-01", "end": "2024-12-31", "val": 1.8, "accn": "c", "form": "10-K/A", "filed": "2026-03-01"},
                        ]
                    }
                },
                "AssetsCurrent": {
                    "units": {
                        "USD": [
                            {"end": "2024-12-31", "val": 10, "accn": "a", "form": "10-K", "filed": "2025-02-01"},
                            {"end": "2023-12-31", "val": 9, "accn": "a", "form": "10-K", "filed": "2025-02-01"},
                        ]
                    }
                },
            }
        }
    }
    rows = extract_facts("0000000001", payload)
    eps = [r for r in rows if r["concept"] == "EarningsPerShareDiluted"]
    assert [(r["accn"], r["value"]) for r in eps] == [("a", 2.0), ("c", 1.8)]  # quarter dropped, unchanged repeat dropped
    ca = [r for r in rows if r["concept"] == "AssetsCurrent"]
    assert [r["period_end"] for r in ca] == ["2024-12-31"]  # prior-year comparative dropped


def test_mda_extraction_skips_table_of_contents():
    text = (
        "Item 7. Management's Discussion and Analysis 24\nItem 7A. Quantitative 30\n"
        + "filler " * 50
        + "\nItem 7. Management's Discussion and Analysis\n"
        + "Real discussion. " * 300
        + "\nItem 7A. Quantitative and Qualitative Disclosures"
    )
    out = extract_section(text, "10-K", 40000)
    assert out.startswith("Item 7. Management's Discussion and Analysis\nReal discussion.")
    assert "Item 7A" not in out

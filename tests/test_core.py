from pathlib import Path

import pytest

from amia import stats
from amia.data.edgar import extract_facts, extract_section
from amia.data.universe import normalize, parse_constituents
from amia.decision import composite, confidence, extract_numbers, grounding_check, text_score
from amia.frameworks import graham
from amia.llm import template_thesis

FIXTURES = Path(__file__).parent / "fixtures"


def _facts(eps: list[float], price_div=0.5, ca=300.0, cl=100.0, ltd=150.0, equity=1000.0, shares=100.0):
    rows = []
    for i, e in enumerate(eps):
        year = 2015 + i
        filed = f"{year + 1}-02-15"
        accn = f"a{year}"
        for concept, value, start in [
            ("EarningsPerShareDiluted", e, f"{year}-01-01"),
            ("Revenues", 1e9, f"{year}-01-01"),
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
    assert res.screen_pass
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


def test_scores_scale():
    assert graham.value_score(None) == 0 and graham.value_score(-0.1) == 0
    assert graham.value_score(0.15) == pytest.approx(2.0)
    assert graham.value_score(0.6) == 4.0
    assert text_score([]) == 2.0
    assert text_score([{"sentiment": 4, "materiality": 4, "novelty": 4}]) == pytest.approx(4.0)
    assert composite(4, 4, 4) == pytest.approx(4.0)


def test_confidence_prior_then_isotonic():
    assert confidence(3.6, None) == (0.6, "prior")
    steps = stats.isotonic_fit([1, 2, 3, 4], [0, 0, 1, 1])
    assert confidence(3.5, steps)[1] == "isotonic"


def test_number_grounding():
    facts = {"price": 16.2, "iv_low": 21.3, "mos_band_low": 0.2394, "criteria_passed": 7, "criteria_total": 8, "score_max": 4}
    ok, bad = grounding_check(["Price $16.20 is 24% below 21.30; passes 7 of 8 criteria on a 0-4 scale per the 10-K."], facts)
    assert ok, bad
    ok, bad = grounding_check(["Price 16.20 implies a 31% margin of safety."], facts)
    assert not ok and bad == ["31%"]


def test_extract_numbers_ignores_tickers_and_forms():
    nums = [v for _, v, _ in extract_numbers("MK05 filed a 10-Q; EPS rose to 1.25 (up 3%).")]
    assert nums == [1.25, 3.0]


def test_template_thesis_always_grounded():
    facts = {
        "price": 10.0, "iv_low": 14.0, "iv_mid": 16.0, "iv_high": 18.0, "mos_band_low": 0.2857, "criteria_passed": 8,
        "criteria_total": 8, "composite_score": 3.4, "threshold": 2.75, "text_score": 2.5, "text_items": 3,
        "target_weight": 0.025, "score_max": 4,
    }
    for action in ("ACCUMULATE", "EXIT"):
        ok, bad = grounding_check(template_thesis(action, "XYZ", facts), facts)
        assert ok, (action, bad)


def test_stats():
    assert stats.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert stats.max_drawdown([100, 120, 90, 130]) == pytest.approx(-0.25)
    a, b, _, _ = stats.ols([0.01, -0.02, 0.03, 0.0, 0.01], [0.02, -0.04, 0.06, 0.0, 0.02])
    assert b == pytest.approx(2.0) and a == pytest.approx(0.0, abs=1e-12)
    steps = stats.isotonic_fit([1, 2, 3], [1, 0, 1])
    assert [v for _, v in steps] == [0.5, 1.0]


def test_universe_parser():
    rows = parse_constituents((FIXTURES / "sp500_snippet.html").read_text())
    assert len(rows) == 5
    assert rows[0]["symbol"] and rows[0]["sector"]
    assert normalize("BRK-B") == normalize("BRK.B") == "BRKB"


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

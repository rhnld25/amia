"""SEC EDGAR: ticker->CIK map, XBRL company facts (point-in-time), recent filings and filing text."""

from __future__ import annotations

import re
from datetime import date
from html.parser import HTMLParser
from typing import Any

from .http import HttpClient

DURATION_CONCEPTS = [
    "EarningsPerShareDiluted",
    "EarningsPerShareBasic",
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "NetIncomeLoss",
    "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "CommonStockDividendsPerShareDeclared",
    "CommonStockDividendsPerShareCashPaid",
    "PaymentsOfDividendsCommonStock",
    "PaymentsOfDividends",
]
INSTANT_CONCEPTS = [
    "AssetsCurrent",
    "LiabilitiesCurrent",
    "LongTermDebtNoncurrent",
    "LongTermDebt",
    "StockholdersEquity",
    "CommonStockSharesOutstanding",
]
DEI_INSTANT = ["EntityCommonStockSharesOutstanding"]
SCORED_FORMS = {"10-K", "10-Q", "8-K"}
MIN_YEAR = 2008


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def extract_facts(cik: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep annual durations (with restatements only when the value changed) and current-period instants."""
    rows: list[dict[str, Any]] = []
    facts = payload.get("facts", {})
    wanted = [("us-gaap", c, "duration") for c in DURATION_CONCEPTS]
    wanted += [("us-gaap", c, "instant") for c in INSTANT_CONCEPTS]
    wanted += [("dei", c, "instant") for c in DEI_INSTANT]
    for taxonomy, concept, kind in wanted:
        units = facts.get(taxonomy, {}).get(concept, {}).get("units", {})
        for entries in units.values():
            seen: dict[str, float] = {}
            for e in sorted(entries, key=lambda e: e.get("filed", "")):
                form = e.get("form", "")
                end, filed = e.get("end"), e.get("filed")
                if not end or not filed or not form.startswith(("10-K", "10-Q")) or int(end[:4]) < MIN_YEAR:
                    continue
                if kind == "duration":
                    start = e.get("start")
                    if not start or not 350 <= _days(start, end) <= 380:
                        continue
                    if seen.get(end) == e["val"]:
                        continue
                    seen[end] = e["val"]
                else:
                    start = None
                    if _days(end, filed) > 120:
                        continue
                rows.append(
                    {
                        "cik": cik,
                        "concept": concept,
                        "period_start": start,
                        "period_end": end,
                        "value": float(e["val"]),
                        "filed": filed,
                        "form": form,
                        "accn": e["accn"],
                    }
                )
    # identical (concept, period_end, accn) can appear under several units; keep the first
    dedup: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        dedup.setdefault((r["concept"], r["period_end"], r["accn"]), r)
    return list(dedup.values())


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "ix:header"):
            self._skip += 1
        elif tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "ix:header") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


_MDA = re.compile(r"item\s+(?:7|2)\s*[\.:\-–—]?\s*management[’'`s]{0,2}\s+discussion\s+and\s+analysis", re.I)
_MDA_END = re.compile(r"^\s*item\s+(?:7a|8|3|4)\b", re.I | re.M)


def extract_section(text: str, form: str, max_chars: int) -> str:
    """MD&A for 10-K/10-Q (the longest heading-to-next-item span, which skips the table of contents)."""
    if form in ("10-K", "10-Q"):
        best, best_len = "", 0
        for m in _MDA.finditer(text):
            end = _MDA_END.search(text, m.end())
            stop = end.start() if end else len(text)
            if stop - m.start() > best_len:
                best, best_len = text[m.start() : stop], stop - m.start()
        if best_len > 2000:
            return best[:max_chars]
    return text[:max_chars]


class Edgar:
    def __init__(self, contact_email: str):
        self.http = HttpClient(
            headers={"User-Agent": f"AMIA research {contact_email}", "Accept-Encoding": "gzip, deflate"},
            min_interval=0.12,  # SEC fair access: at most 10 requests per second
        )
        self._submissions: dict[str, dict[str, Any]] = {}

    def submissions(self, cik: str) -> dict[str, Any]:
        if cik not in self._submissions:
            self._submissions[cik] = self.http.json(f"https://data.sec.gov/submissions/CIK{cik}.json")
        return self._submissions[cik]

    def company_profile(self, cik: str) -> dict[str, Any]:
        d = self.submissions(cik)
        return {"name": d.get("name"), "sic": d.get("sic"), "sic_description": d.get("sicDescription")}

    def ticker_map(self) -> dict[str, dict[str, Any]]:
        data = self.http.json("https://www.sec.gov/files/company_tickers.json")
        out = {}
        for row in data.values():
            key = re.sub(r"[^A-Z0-9]", "", row["ticker"].upper())
            out.setdefault(key, {"cik": str(row["cik_str"]).zfill(10), "ticker": row["ticker"], "title": row["title"]})
        return out

    def company_facts(self, cik: str) -> list[dict[str, Any]]:
        try:
            payload = self.http.json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        except Exception as e:
            if "404" in str(e):
                return []
            raise
        return extract_facts(cik, payload)

    def recent_filings(self, cik: str, since: str) -> list[dict[str, Any]]:
        r = self.submissions(cik).get("filings", {}).get("recent", {})
        out = []
        for i, form in enumerate(r.get("form", [])):
            if form not in SCORED_FORMS or r["filingDate"][i] < since:
                continue
            out.append(
                {
                    "accn": r["accessionNumber"][i],
                    "form": form,
                    "filed": r["filingDate"][i],
                    "accepted": r["acceptanceDateTime"][i],
                    "primary_doc": r["primaryDocument"][i],
                }
            )
        return out

    def filing_url(self, cik: str, accn: str, primary_doc: str) -> str:
        return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/{primary_doc}"

    def filing_text(self, cik: str, accn: str, primary_doc: str, form: str) -> str:
        html = self.http.get(self.filing_url(cik, accn, primary_doc)).text
        max_chars = 40_000 if form in ("10-K", "10-Q") else 15_000
        return extract_section(html_to_text(html), form, max_chars)

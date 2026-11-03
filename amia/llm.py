"""LLM access: batched rubric scoring and thesis prose, with spend tracking and a hard monthly cap.

The LLM never produces numbers that drive decisions. It returns rubric integers (0-4) and prose.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from importlib import resources
from typing import Any

from .config import MODEL_PRICES, Settings
from .db import DB

log = logging.getLogger(__name__)

RUBRIC_VERSION = "text_scoring_v1"

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "novelty": {"type": "integer", "enum": [0, 1, 2, 3, 4]},
        "sentiment": {"type": "integer", "enum": [0, 1, 2, 3, 4]},
        "materiality": {"type": "integer", "enum": [0, 1, 2, 3, 4]},
        "rationale": {"type": "string"},
        "citation": {"type": "string"},
    },
    "required": ["novelty", "sentiment", "materiality", "rationale", "citation"],
    "additionalProperties": False,
}

THESIS_SCHEMA = {
    "type": "object",
    "properties": {"bullets": {"type": "array", "items": {"type": "string"}}},
    "required": ["bullets"],
    "additionalProperties": False,
}

THESIS_SYSTEM = (
    "You write investment thesis bullets for a paper-trading research system. The decision has already "
    "been made by code; you only explain it. Write exactly 3 bullets, each at most 30 words. Use only "
    "numbers that appear in the FACTS object, written exactly as given there or as a percentage of a "
    "fraction (0.24 may be written as 24%). Do not mention dates or years, do not invent figures, and do "
    "not give advice beyond the stated action."
)


def rubric_text() -> str:
    return resources.files("amia").joinpath(f"rubrics/{RUBRIC_VERSION}.md").read_text()


def cost_usd(model: str, usage: Any, batch: bool) -> float:
    p = MODEL_PRICES.get(model, MODEL_PRICES["claude-haiku-5-5"])
    usd = (
        (getattr(usage, "input_tokens", 0) or 0) * p["input"]
        + (getattr(usage, "output_tokens", 0) or 0) * p["output"]
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p["cache_read"]
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * p["cache_write"]
    ) / 1e6
    return usd * (0.5 if batch else 1.0)


class BudgetExceeded(RuntimeError):
    pass


class LLM:
    def __init__(self, settings: Settings, db: DB):
        self.settings = settings
        self.db = db
        self.fake = settings.mock or not settings.anthropic_api_key
        self._client = None

    @property
    def client(self):  # lazy so local/mock runs never need the SDK configured
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    # ---- spend -------------------------------------------------------------
    def month_spend(self) -> float:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return float(self.db.scalar("SELECT COALESCE(SUM(usd), 0) FROM llm_spend WHERE ts LIKE :m", {"m": f"{month}%"}) or 0)

    def check_budget(self, estimate_usd: float) -> None:
        spent = self.month_spend()
        if spent + estimate_usd > self.settings.llm_monthly_cap_usd:
            raise BudgetExceeded(f"LLM spend ${spent:.2f} + est ${estimate_usd:.2f} exceeds cap ${self.settings.llm_monthly_cap_usd:.2f}")

    def _record(self, model: str, purpose: str, usage: Any, batch: bool) -> None:
        self.db.upsert(
            "llm_spend",
            [
                {
                    "id": uuid.uuid4().hex,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "model": model,
                    "purpose": purpose,
                    "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                    "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                    "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
                    "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    "batch": 1 if batch else 0,
                    "usd": cost_usd(model, usage, batch),
                }
            ],
            ["id"],
        )

    # ---- scoring -----------------------------------------------------------
    def _score_params(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": self.settings.scoring_model,
            "max_tokens": 1024,
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCORE_SCHEMA}},
            "system": [{"type": "text", "text": rubric_text(), "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": f"ITEM TYPE: {item['item_type']}\n\n<item>\n{item['body']}\n</item>"}],
        }

    def submit_scoring_batch(self, items: list[dict[str, Any]]) -> str:
        """items: {custom_id, item_type, body}. Returns a batch id (or 'fake-...' in fake mode)."""
        est_tokens = sum(len(i["body"]) for i in items) / 3.5 + 600 * len(items)
        self.check_budget(est_tokens * MODEL_PRICES[self.settings.scoring_model]["input"] / 1e6)
        if self.fake:
            batch_id = "fake-" + uuid.uuid4().hex[:12]
            self.db.set_kv(f"fake_batch:{batch_id}", [{"custom_id": i["custom_id"], "body": i["body"]} for i in items])
            return batch_id
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        batch = self.client.messages.batches.create(
            requests=[Request(custom_id=i["custom_id"], params=MessageCreateParamsNonStreaming(**self._score_params(i))) for i in items]
        )
        return batch.id

    def collect_batch(self, batch_id: str, wait_minutes: float = 0) -> dict[str, dict[str, Any]] | None:
        """Returns {custom_id: parsed_score_or_{'error':...}} when ended, else None."""
        if batch_id.startswith("fake-"):
            items = self.db.get_kv(f"fake_batch:{batch_id}", [])
            return {i["custom_id"]: fake_score(i["body"]) for i in items}
        deadline = time.monotonic() + wait_minutes * 60
        while True:
            batch = self.client.messages.batches.retrieve(batch_id)
            if batch.processing_status == "ended":
                break
            if time.monotonic() >= deadline:
                return None
            time.sleep(30)
        out: dict[str, dict[str, Any]] = {}
        for result in self.client.messages.batches.results(batch_id):
            if result.result.type != "succeeded":
                out[result.custom_id] = {"error": result.result.type}
                continue
            msg = result.result.message
            self._record(msg.model, "scoring", msg.usage, batch=True)
            if msg.stop_reason == "refusal":
                out[result.custom_id] = {"error": "refusal"}
                continue
            text = next((b.text for b in msg.content if b.type == "text"), "")
            try:
                out[result.custom_id] = validate_score(json.loads(text))
            except (ValueError, KeyError) as e:
                out[result.custom_id] = {"error": f"invalid: {e}"}
        return out

    # ---- thesis ------------------------------------------------------------
    def write_thesis(self, action: str, symbol: str, facts: dict[str, Any]) -> list[str]:
        if self.fake:
            return template_thesis(action, symbol, facts)
        self.check_budget(0.01)
        prompt = f"ACTION: {action}\nTICKER: {symbol}\nFACTS: {json.dumps(facts, sort_keys=True)}"
        msg = self.client.messages.create(
            model=self.settings.thesis_model,
            max_tokens=1024,
            thinking={"type": "disabled"},
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": THESIS_SCHEMA}},
            system=THESIS_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        self._record(msg.model, "thesis", msg.usage, batch=False)
        if msg.stop_reason == "refusal":
            raise RuntimeError("thesis refused")
        text = next((b.text for b in msg.content if b.type == "text"), "{}")
        return [str(b) for b in json.loads(text).get("bullets", [])][:3]


def validate_score(d: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k in ("novelty", "sentiment", "materiality"):
        v = int(d[k])
        if not 0 <= v <= 4:
            raise ValueError(f"{k}={v} out of range")
        out[k] = v
    out["rationale"] = str(d.get("rationale", ""))[:400]
    out["citation"] = str(d.get("citation", ""))[:300]
    return out


_POS = re.compile(r"beats|raises|record|wins|expanding|strong|growth", re.I)
_NEG = re.compile(r"recall|impairment|weaker|departure|cut|loss|investigation", re.I)


def fake_score(body: str) -> dict[str, Any]:
    pos, neg = len(_POS.findall(body)), len(_NEG.findall(body))
    sentiment = 2 + min(2, pos) - min(2, neg)
    return {
        "novelty": 2 + (1 if pos or neg else 0),
        "sentiment": max(0, min(4, sentiment)),
        "materiality": 2 if (pos or neg) else 1,
        "rationale": "Keyword-based fake score (mock mode).",
        "citation": body[:120],
    }


def _fmt(v: Any) -> str:
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def template_thesis(action: str, symbol: str, f: dict[str, Any]) -> list[str]:
    """Deterministic fallback thesis; always passes the number-grounding check."""
    if action == "EXIT":
        return [
            f"{symbol} exit: price {_fmt(f['price'])} versus intrinsic value midpoint {_fmt(f['iv_mid'])}.",
            f"Graham criteria passed: {f['criteria_passed']} of {f['criteria_total']}.",
            f"Composite score {_fmt(f['composite_score'])} on a 0-{f['score_max']} scale.",
        ]
    return [
        f"Price {_fmt(f['price'])} sits below the intrinsic value band low of {_fmt(f['iv_low'])}, a margin of safety of {f['mos_band_low'] * 100:.0f}%.",
        f"Passes {f['criteria_passed']} of {f['criteria_total']} Graham defensive criteria; composite score {_fmt(f['composite_score'])} cleared threshold {_fmt(f['threshold'])}.",
        f"Text signal score {_fmt(f['text_score'])} from {f['text_items']} recent scored items; target weight {_fmt(f['target_weight'])}.",
    ]

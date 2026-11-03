"""Storage: SQLite locally, Postgres (Supabase) in the cloud, same SQL for both.

Tables are append-mostly. Decisions, snapshots and facts are never updated in place.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

_SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (
    month TEXT NOT NULL, symbol TEXT NOT NULL, cik TEXT, name TEXT, sector TEXT, index_name TEXT,
    PRIMARY KEY (month, symbol));
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL, date TEXT NOT NULL, open {REAL}, close {REAL}, ret {REAL},
    PRIMARY KEY (symbol, date));
CREATE INDEX IF NOT EXISTS prices_date ON prices (date);
CREATE TABLE IF NOT EXISTS facts (
    cik TEXT NOT NULL, concept TEXT NOT NULL, period_start TEXT, period_end TEXT NOT NULL,
    value {REAL}, filed TEXT NOT NULL, form TEXT, accn TEXT NOT NULL,
    PRIMARY KEY (cik, concept, period_end, accn));
CREATE INDEX IF NOT EXISTS facts_cik ON facts (cik);
CREATE TABLE IF NOT EXISTS facts_status (
    cik TEXT PRIMARY KEY, fetched_at TEXT, last_filed TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS filings (
    accn TEXT PRIMARY KEY, cik TEXT, symbol TEXT, form TEXT, filed TEXT, accepted TEXT,
    primary_doc TEXT, url TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, symbols TEXT, created_at TEXT, headline TEXT, summary TEXT, url TEXT,
    status TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS text_items (
    item_id TEXT PRIMARY KEY, item_type TEXT, symbols TEXT, published_at TEXT, body {BLOB},
    status TEXT, batch_id TEXT, recheck INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS text_scores (
    item_id TEXT NOT NULL, score_pass TEXT NOT NULL, item_type TEXT, symbols TEXT, published_at TEXT,
    novelty INTEGER, sentiment INTEGER, materiality INTEGER, rationale TEXT, citation TEXT,
    model TEXT, rubric_version TEXT, scored_at TEXT,
    PRIMARY KEY (item_id, score_pass));
CREATE TABLE IF NOT EXISTS llm_batches (
    batch_id TEXT PRIMARY KEY, kind TEXT, created_at TEXT, status TEXT, n_requests INTEGER);
CREATE TABLE IF NOT EXISTS llm_spend (
    id TEXT PRIMARY KEY, ts TEXT, model TEXT, purpose TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cache_read INTEGER, cache_write INTEGER, batch INTEGER, usd {REAL});
CREATE TABLE IF NOT EXISTS macro (
    series TEXT NOT NULL, date TEXT NOT NULL, value {REAL}, PRIMARY KEY (series, date));
CREATE TABLE IF NOT EXISTS scores (
    date TEXT NOT NULL, symbol TEXT NOT NULL, composite {REAL}, value_score {REAL}, quality_score {REAL},
    text_score {REAL}, mos_low {REAL}, iv_mid {REAL}, price {REAL}, criteria_passed INTEGER,
    PRIMARY KEY (date, symbol));
CREATE TABLE IF NOT EXISTS snapshots (
    hash TEXT PRIMARY KEY, date TEXT, created_at TEXT, data {BLOB});
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY, as_of TEXT, date TEXT, symbol TEXT, action TEXT, target_weight {REAL},
    confidence {REAL}, record TEXT, snapshot_hash TEXT, resolve_on TEXT, thesis_ok INTEGER);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY, decision_id TEXT, symbol TEXT, side TEXT, target_weight {REAL},
    created_date TEXT, status TEXT, fill_date TEXT, fill_price {REAL}, amount {REAL});
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY, sector TEXT, opened_date TEXT, entry_price {REAL}, value {REAL},
    cost {REAL}, decision_id TEXT, last_close {REAL});
CREATE TABLE IF NOT EXISTS closed_positions (
    id TEXT PRIMARY KEY, symbol TEXT, sector TEXT, opened_date TEXT, closed_date TEXT,
    entry_price {REAL}, exit_price {REAL}, ret {REAL}, excess_ret {REAL}, decision_id TEXT, tags TEXT);
CREATE TABLE IF NOT EXISTS nav (
    date TEXT PRIMARY KEY, nav {REAL}, cash {REAL}, invested {REAL}, ret {REAL},
    spy_ret {REAL}, universe_ret {REAL}, rf_ret {REAL}, turnover {REAL});
CREATE TABLE IF NOT EXISTS ic (
    date TEXT NOT NULL, horizon INTEGER NOT NULL, ic {REAL}, n INTEGER, PRIMARY KEY (date, horizon));
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, kind TEXT, as_of TEXT, started TEXT, finished TEXT, status TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, url: str):
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        self.url = url
        self.engine: Engine = create_engine(url, future=True, pool_pre_ping=True)
        self.is_sqlite = self.engine.dialect.name == "sqlite"

    def init_schema(self) -> None:
        types = {"REAL": "REAL", "BLOB": "BLOB"} if self.is_sqlite else {"REAL": "DOUBLE PRECISION", "BLOB": "BYTEA"}
        ddl = _SCHEMA.format(**types)
        with self.engine.begin() as conn:
            for stmt in ddl.split(";"):
                if stmt.strip():
                    conn.execute(text(stmt))

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(sql), params or {})

    def executemany(self, sql: str, rows: Sequence[dict[str, Any]], chunk: int = 2000) -> None:
        if not rows:
            return
        with self.engine.begin() as conn:
            for i in range(0, len(rows), chunk):
                conn.execute(text(sql), list(rows[i : i + chunk]))

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text(sql), params or {})]

    def scalar(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params or {}).scalar()

    def upsert(self, table: str, rows: Iterable[dict[str, Any]], keys: Sequence[str], update: bool = True) -> None:
        rows = list(rows)
        if not rows:
            return
        cols = list(rows[0].keys())
        placeholders = ", ".join(f":{c}" for c in cols)
        conflict = ", ".join(keys)
        non_keys = [c for c in cols if c not in keys]
        if update and non_keys:
            action = "DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in non_keys)
        else:
            action = "DO NOTHING"
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) ON CONFLICT ({conflict}) {action}"
        self.executemany(sql, rows)

    def get_kv(self, key: str, default: Any = None) -> Any:
        v = self.scalar("SELECT value FROM kv WHERE key = :k", {"k": key})
        return json.loads(v) if v is not None else default

    def set_kv(self, key: str, value: Any) -> None:
        self.upsert("kv", [{"key": key, "value": json.dumps(value)}], ["key"])

    def size_mb(self) -> float | None:
        if self.is_sqlite:
            return None
        return float(self.scalar("SELECT pg_database_size(current_database())")) / 1e6

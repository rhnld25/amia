"""Content-hashed, immutable snapshots of every input a decision used."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .db import DB


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def save_snapshot(db: DB, date: str, data: dict[str, Any]) -> str:
    raw = canonical(data)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    db.upsert(
        "snapshots",
        [{"hash": digest, "date": date, "created_at": datetime.now(timezone.utc).isoformat(), "data": gzip.compress(raw)}],
        ["hash"],
        update=False,
    )
    return digest


def load_snapshot(db: DB, digest: str) -> dict[str, Any] | None:
    rows = db.query("SELECT data FROM snapshots WHERE hash = :h", {"h": digest})
    if not rows:
        return None
    return json.loads(gzip.decompress(bytes(rows[0]["data"])))

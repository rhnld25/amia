"""Command line: python -m amia <command>."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import BENCHMARKS, Settings


def _today_ny() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(prog="amia")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    d = sub.add_parser("daily", help="run the daily pipeline")
    d.add_argument("--date", default=None)
    d.add_argument("--facts-limit", type=int, default=200)
    i = sub.add_parser("intraday", help="refresh news and the live watchlist; no decisions or trades")
    i.add_argument("--date", default=None)
    b = sub.add_parser("backfill", help="load macro history and score the last week of news to seed the watchlist")
    b.add_argument("--days", type=int, default=7)
    m = sub.add_parser("monthly", help="write the monthly evaluation report")
    m.add_argument("--date", default=None)
    s = sub.add_parser("build-site", help="render the dashboard to ./site")
    s.add_argument("--out", default="site")
    sim = sub.add_parser("simulate", help="replay the daily pipeline over a date range (mock data only)")
    sim.add_argument("--start", required=True)
    sim.add_argument("--end", required=True)
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("AMIA_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()

    if args.cmd == "build-site":
        from .db import DB
        from .site import build_site

        db = DB(settings.database_url)
        db.init_schema()
        build_site(db, args.out)
        return

    from .pipeline import Pipeline

    pipe = Pipeline(settings)
    if args.cmd == "init-db":
        print("schema ready")
    elif args.cmd == "daily":
        print(json.dumps(pipe.daily(args.date or _today_ny(), args.facts_limit), default=str, indent=2))
    elif args.cmd == "intraday":
        print(json.dumps(pipe.intraday(args.date or _today_ny()), default=str, indent=2))
    elif args.cmd == "backfill":
        as_of = _today_ny()
        pipe.ticker_map(as_of)
        pipe.update_prices(BENCHMARKS, as_of)
        pipe.update_macro(as_of)
        since = (date.fromisoformat(as_of) - timedelta(days=args.days)).isoformat() + "T00:00:00Z"
        pipe.ingest_news(as_of, live_clock=True, always=set(), since=since, cap=8000)
        pipe.score_text(wait_minutes=90)
        print(json.dumps({"notes": pipe.notes}, indent=2))
    elif args.cmd == "monthly":
        print(pipe.monthly_report(args.date or _today_ny()))
    elif args.cmd == "simulate":
        if not settings.mock:
            raise SystemExit("simulate only runs with AMIA_MOCK=1")
        from .data.mock import trading_days

        for day in trading_days(date.fromisoformat(args.start), date.fromisoformat(args.end)):
            res = pipe.daily(day.isoformat(), facts_limit=1000, live_clock=False)
            if res.get("decisions"):
                print(day, res["decisions"])
        print(pipe.monthly_report(args.end))


if __name__ == "__main__":
    main()

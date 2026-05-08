"""`migaku-notion export` — write state.db rows to CSV / XLSX. Pure local.

Ported verbatim from v1's `run_export`. The `--with-meaning` path is the
only one that touches Notion, and it's optional.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .. import config
from ..export import (
    export_csv,
    export_xlsx,
    fetch_meanings_from_notion,
    filter_rows,
)
from ..notion_client import NotionClient
from ..state import StateCache


log = logging.getLogger("migaku-notion")


def run(args: argparse.Namespace) -> int:
    if not (args.csv or args.xlsx):
        log.error("Pass at least one of --csv PATH or --xlsx PATH.")
        return 2

    if not config.STATE_DB_PATH.exists():
        log.error("Local cache (%s) not initialised. Run `python -m migaku_notion sync` "
                  "or `python -m migaku_notion rebuild-cache` first.",
                  config.STATE_DB_PATH.name)
        return 1

    with StateCache(config.STATE_DB_PATH) as cache:
        all_rows = list(cache.load_all().values())

    statuses = [s.strip().upper() for s in (args.status or "").split(",") if s.strip()] or None
    if statuses == ["ALL"]:
        statuses = None
    rows = filter_rows(all_rows, args.lang or None, statuses, args.include_archived)
    log.info("Exporting %d rows (filtered from %d cached, lang=%s, status=%s, archived=%s)",
             len(rows), len(all_rows), args.lang or "ALL",
             ",".join(statuses) if statuses else "ALL", args.include_archived)

    meanings: dict[str, str] | None = None
    if args.with_meaning:
        notion_token = config.notion_token()
        notion_db = config.notion_database_id()
        if not (notion_token and notion_db):
            log.error("--with-meaning requires NOTION_TOKEN and NOTION_DATABASE_ID in .env")
            return 2
        notion = NotionClient(notion_token, notion_db)
        meanings = fetch_meanings_from_notion(notion)

    if args.csv:
        export_csv(Path(args.csv), rows, meanings)
    if args.xlsx:
        export_xlsx(Path(args.xlsx), rows, meanings)

    return 0

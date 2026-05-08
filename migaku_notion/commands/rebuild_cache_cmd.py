"""`migaku-notion rebuild-cache` — recreate state.db from a Notion read.

Stubbed only because it depends on the NotionClient being instantiable
with valid env. The v1 implementation is verbatim portable; the only
reason it's not pre-wired here is to keep the scaffold reviewable
without forcing the user to have a working .env.

Once .env is filled in, this command's body is ~15 lines (delete state.db
+ sidecars, run NotionClient.query_all_pages(), upsert each into a fresh
StateCache). See migaku-notion/sync/sync.py::run_rebuild_cache for the
exact reference.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .. import config
from ..notion_client import NotionClient, cache_row_from_notion_page
from ..state import StateCache


log = logging.getLogger("migaku-notion")


def run(args: argparse.Namespace) -> int:
    notion_token = config.notion_token()
    notion_db = config.notion_database_id()
    if not (notion_token and notion_db):
        log.error("NOTION_TOKEN and NOTION_DATABASE_ID must be set in .env")
        return 2

    notion = NotionClient(notion_token, notion_db)
    log.info("Rebuilding %s from Notion (read-only — no Notion writes will occur) ...",
             config.STATE_DB_PATH)

    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(config.STATE_DB_PATH) + suffix)
        if p.exists():
            log.info("  removing %s", p.name)
            p.unlink()

    pages = notion.query_all_pages()
    cache = StateCache(config.STATE_DB_PATH)
    rows = 0
    for page in pages:
        row = cache_row_from_notion_page(page)
        if row is None:
            continue
        cache.upsert(row)
        rows += 1
    cache.close()
    log.info("Rebuilt cache at %s with %d rows.", config.STATE_DB_PATH, rows)
    return 0

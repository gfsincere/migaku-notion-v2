"""`migaku-notion status` — connectivity + cache health check.

Exits non-zero only when core sync prerequisites fail (Migaku reachability).
Notion is optional; if not configured we report "disabled" but keep a healthy
status for local-only workflows and future non-Notion integrations.
"""
from __future__ import annotations

import argparse
import logging

from .. import config
from ..migaku import auth, pull
from ..notion_client import NotionClient
from ..state import StateCache


log = logging.getLogger("migaku-notion")


def run(args: argparse.Namespace) -> int:
    rc = 0

    # --- Migaku reachability ----------------------------------------------
    try:
        session = auth.auth_session_from_env(
            refresh_token=config.migaku_refresh_token(),
            email=config.migaku_email(),
            password=config.migaku_password(),
        )
        device_id = config.get_or_create_device_id()
        # Tiny pull: serverVersion=0 returns the whole payload but we only
        # peek at array sizes, so it's not actually expensive (one call,
        # gzipped on the wire).
        payload = pull.pull_sync(session, device_id, server_version=0)
        sizes = {k: len(v) for k, v in payload.items() if isinstance(v, list)}
        words_for_lang = sum(
            1 for w in (payload.get("words") or [])
            if isinstance(w, dict)
            and not w.get("del")
            and (not args.lang or (w.get("language") or "") == args.lang)
        )
        print("Migaku core-server: OK")
        print(f"  device_id={device_id[:8]}...  next server_version="
              f"{pull.next_server_version(payload, previous=0)}")
        print(f"  words[{args.lang}]: {words_for_lang} (of {sizes.get('words', 0)} total)")
        print(f"  cards: {sizes.get('cards', 0)}, "
              f"reviews: {sizes.get('reviews', 0)}, "
              f"cardWordRelations: {sizes.get('cardWordRelations', 0)}")
    except RuntimeError as exc:
        print(f"Migaku core-server: FAILED ({exc})")
        rc = 1

    # --- Notion reachability ----------------------------------------------
    notion_token = config.notion_token()
    notion_db = config.notion_database_id()
    if notion_token and notion_db:
        try:
            notion = NotionClient(notion_token, notion_db)
            info = notion.get_database()
            title = "".join(
                t.get("plain_text", "") for t in (info.get("title") or [])
            ) or "(untitled)"
            props = list((info.get("properties") or {}).keys())
            v2_present = [n for n in ("Frequency", "Example") if n in props]
            print(f"Notion: OK — '{title}' ({len(props)} columns)")
            if v2_present:
                print(f"  v2 columns present: {', '.join(v2_present)}")
            else:
                print("  v2 columns MISSING (Frequency, Example) — run `setup` "
                      "to upgrade the schema.")
        except RuntimeError as exc:
            print(f"Notion: FAILED ({exc})")
            rc = 1
    else:
        print("Notion: DISABLED (optional) — NOTION_TOKEN and/or "
              "NOTION_DATABASE_ID missing from .env.")

    # --- Local cache stats ------------------------------------------------
    if config.STATE_DB_PATH.exists():
        with StateCache(config.STATE_DB_PATH) as cache:
            s = cache.stats()
            sv = cache.get_server_version()
            first = cache.is_v2_first_sync_done()
            cache_device = cache.get_device_id()
        print(f"local cache ({config.STATE_DB_PATH.name}):")
        print(f"  total cached rows:     {s['total']}")
        print(f"  archived rows:         {s['archived']}")
        print(f"  newest last_synced:    {s['last_synced'] or '(never)'}")
        print(f"  server_version:        {sv}")
        print(f"  v2_first_sync_done:    {first}")
        if cache_device:
            print(f"  device_id (cache):     {cache_device[:8]}... "
                  f"({'matches' if cache_device == config.migaku_device_id() else 'DIFFERS from .env'})")
    else:
        print(f"local cache ({config.STATE_DB_PATH.name}): not initialised — "
              "first `sync` will create it (or run `rebuild-cache`)")

    return rc

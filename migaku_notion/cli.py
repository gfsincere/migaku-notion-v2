"""argparse dispatcher. Mirrors v1's CLI surface 1:1.

Subcommands: sync, rebuild-cache, login, status, chars, setup, export.
Same flag names, same defaults — so anyone who scripted around v1's CLI
can drop v2 in unchanged.
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import config


def _force_utf8_stdout() -> None:
    """Make print(<hanzi>) work on Windows consoles whose default code page
    is cp1252.

    Without this, `chars --list` (and any future command that prints CJK)
    crashes with UnicodeEncodeError on a fresh PowerShell. Modern Pythons
    expose `reconfigure` on the standard streams; older ones silently
    no-op which is also fine.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
from .commands import (
    chars_cmd,
    export_cmd,
    login_cmd,
    rebuild_cache_cmd,
    setup_cmd,
    status_cmd,
    sync_cmd,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migaku-notion",
        description=(
            "Mirror your Migaku vocabulary into a Notion database (v2: direct "
            "Migaku API, no Docker / Go middleman). Same Notion schema and "
            "same `state.db` cache as v1; drop-in replacement."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser(
        "sync",
        help="Sync Migaku words into Notion. Diffs against state.db so re-runs "
             "only PATCH rows whose tracked fields actually changed.",
    )
    p_sync.add_argument("--lang", default=config.DEFAULT_LANG,
                        help=f"Migaku language code (default: {config.DEFAULT_LANG})")
    p_sync.add_argument("--status", default=config.DEFAULT_STATUS,
                        help="Comma-separated migaku statuses to include "
                             "(KNOWN, LEARNING, UNKNOWN, IGNORED). Use 'ALL' or "
                             "empty to include everything. "
                             f"Default: {config.DEFAULT_STATUS}")
    p_sync.add_argument("--dry-run", action="store_true",
                        help="Don't write to Notion or state.db; just log what would happen.")
    p_sync.add_argument("--archive-stale", action="store_true",
                        help="Archive Notion rows that no longer exist in Migaku "
                             "for this lang+status filter")
    p_sync.add_argument("--full-refresh", action="store_true",
                        help="Pull /pull-sync with serverVersion=0 (the whole "
                             "dataset) instead of resuming from the last "
                             "server_version persisted in state.db. Useful for "
                             "daily divergence checks; combined with the diff "
                             "cache, Notion API calls stay minimal because "
                             "only rows whose tracked fields actually changed "
                             "get PATCHed.")
    p_sync.add_argument("--no-dict-meanings", action="store_true",
                        help="Do NOT auto-populate the Notion `Meaning` column "
                             "from Migaku's published dictionary, even on the "
                             "first v2 sync. Use this if you want v2 to behave "
                             "exactly like v1 (Meaning always stays blank "
                             "until you fill it yourself / via Notion AI). "
                             "Has no effect once the first-sync window has "
                             "already passed (state.db meta v2_first_sync_done=1).")
    p_sync.add_argument("--no-notion", action="store_true",
                        help="Disable Notion for this run and sync to local "
                             "state.db only. Useful when building alternative "
                             "integration sinks (Sheets/Airtable/custom).")
    p_sync.set_defaults(func=sync_cmd.run)

    p_rebuild = sub.add_parser(
        "rebuild-cache",
        help="Delete state.db and rebuild it from a fresh Notion query. "
             "Read-only — no Notion writes. Use after manual edits in the "
             "Notion UI or if state.db gets corrupted.",
    )
    p_rebuild.set_defaults(func=rebuild_cache_cmd.run)

    p_login = sub.add_parser(
        "login",
        help="Derive a Migaku refresh token via Firebase email-password login.",
    )
    p_login.add_argument("--email")
    p_login.add_argument("--password")
    p_login.set_defaults(func=login_cmd.run)

    p_status = sub.add_parser(
        "status",
        help="Show Migaku connectivity, word counts, and local-cache stats.",
    )
    p_status.add_argument("--lang", default=config.DEFAULT_LANG)
    p_status.set_defaults(func=status_cmd.run)

    p_chars = sub.add_parser(
        "chars",
        help="Report unique Hanzi character counts from the local cache, "
             "broken down by Migaku status. Useful for tracking HSK progress.",
    )
    p_chars.add_argument("--lang", default=config.DEFAULT_LANG)
    p_chars.add_argument("--list", action="store_true",
                         help="Also print the full sorted list of KNOWN+LEARNING chars.")
    p_chars.set_defaults(func=chars_cmd.run)

    p_setup = sub.add_parser(
        "setup",
        help="Interactive first-run wizard. Walks through Migaku login, "
             "Notion integration setup, parent page selection, auto-creates "
             "the Migaku Vocab database with the right schema, and writes "
             "everything to .env.",
    )
    p_setup.add_argument("--force", action="store_true",
                         help="Re-prompt for every value, even ones already in .env. "
                              "Also creates a NEW Notion database, even if "
                              "NOTION_DATABASE_ID is already set.")
    p_setup.set_defaults(func=setup_cmd.run)

    p_export = sub.add_parser(
        "export",
        help="Export the local cache to CSV or XLSX. Reads from state.db "
             "(no Notion API calls unless --with-meaning is set).",
    )
    p_export.add_argument("--csv", metavar="PATH",
                          help="Write a CSV file at PATH (UTF-8 with BOM, Excel-compatible).")
    p_export.add_argument("--xlsx", metavar="PATH",
                          help="Write an Excel workbook at PATH (frozen header + auto-filter).")
    p_export.add_argument("--lang", default=config.DEFAULT_LANG,
                          help=f"Filter by language code (default: {config.DEFAULT_LANG}). "
                               "Pass empty to include all languages.")
    p_export.add_argument("--status", default=config.DEFAULT_STATUS,
                          help="Comma-separated statuses to include, or ALL. "
                               f"Default: {config.DEFAULT_STATUS}")
    p_export.add_argument("--include-archived", action="store_true",
                          help="Also include rows that have been archived in Notion.")
    p_export.add_argument("--with-meaning", action="store_true",
                          help="Also pull the Meaning column from Notion (one extra "
                               "API query, ~5 sec for 1500 rows). Without this flag, "
                               "the Meaning column in exports will be blank.")
    p_export.set_defaults(func=export_cmd.run)

    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    return args.func(args)

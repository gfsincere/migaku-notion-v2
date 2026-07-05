"""`migaku-notion progress` — KNOWN word / Hanzi char totals over time.

Reads the `progress_snapshots` table in state.db (one row per calendar day,
updated automatically at the end of each non-dry-run `sync`).

Use `progress --serve` to open the local progress dashboard.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date

from .. import config
from ..dashboard_server import serve_dashboard
from ..progress_stats import build_progress_payload, delta_since, pace_per_day
from ..state import ProgressSnapshot, StateCache


log = logging.getLogger("migaku-notion")


def _print_table(snapshots: list[ProgressSnapshot]) -> None:
    print(f"\nProgress snapshots (lang={snapshots[0].lang})")
    print("-" * 56)
    print(f"  {'Date':<12} {'Known words':>12} {'Known chars':>12}  {'Δ words':>8} {'Δ chars':>8}")
    print(f"  {'-' * 12} {'-' * 12} {'-' * 12}  {'-' * 8} {'-' * 8}")
    prev: ProgressSnapshot | None = None
    for snap in snapshots:
        dw = snap.known_words - prev.known_words if prev else 0
        dc = snap.known_chars - prev.known_chars if prev else 0
        dw_s = f"{dw:+d}" if prev else "—"
        dc_s = f"{dc:+d}" if prev else "—"
        print(
            f"  {snap.snapshot_date:<12} {snap.known_words:>12} {snap.known_chars:>12}  "
            f"{dw_s:>8} {dc_s:>8}"
        )
        prev = snap


def _print_rates(snapshots: list[ProgressSnapshot]) -> None:
    latest = snapshots[-1]
    print(f"\nLatest ({latest.snapshot_date}): {latest.known_words} known words, "
          f"{latest.known_chars} unique KNOWN Hanzi")
    for label, days in (("7-day", 7), ("30-day", 30)):
        pace = pace_per_day(delta_since(snapshots, days=days))
        if pace is None:
            continue
        print(
            f"  ~{label} pace ({pace['days']}d): "
            f"{pace['words_per_day']:+.1f} words/day, "
            f"{pace['chars_per_day']:+.2f} chars/day"
        )


def run(args: argparse.Namespace) -> int:
    if args.serve:
        return serve_dashboard(
            host=args.host,
            port=args.port,
            lang=args.lang,
            open_browser=not args.no_open,
        )

    if not config.STATE_DB_PATH.exists():
        log.error("Local cache (%s) not initialised. Run `sync` first.",
                  config.STATE_DB_PATH.name)
        return 1

    with StateCache(config.STATE_DB_PATH) as cache:
        if args.record:
            today = date.today().isoformat()
            snap = cache.record_progress_snapshot(args.lang, today)
            log.info("Recorded %s: known_words=%d known_chars=%d",
                     today, snap.known_words, snap.known_chars)

        snapshots = cache.list_progress_snapshots(args.lang)

    if not snapshots:
        print("No progress snapshots yet. Run `sync` (non-dry-run) or "
              "`progress --record` after your cache has KNOWN words.")
        return 0

    if args.csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(["snapshot_date", "lang", "known_words", "known_chars"])
        for snap in snapshots:
            writer.writerow([snap.snapshot_date, snap.lang, snap.known_words, snap.known_chars])
        return 0

    if args.json:
        import json
        print(json.dumps(build_progress_payload(snapshots, lang=args.lang), indent=2))
        return 0

    _print_table(snapshots)
    _print_rates(snapshots)
    print()
    return 0

"""`migaku-notion hsk` — compare KNOWN words to HSK 2.0 / 3.0 syllabi."""
from __future__ import annotations

import argparse
import json
import logging

from .. import config
from ..hsk import build_hsk_report_from_cache, ensure_hsk_lists
from ..state import StateCache


log = logging.getLogger("migaku-notion")


def _print_standard(report: dict) -> None:
    est = report.get("estimated_level")
    est_s = f"Level {est}" if est is not None else "below Level 1"
    print(f"\n{report['label']}  (estimated: {est_s} at {report['threshold_pct']}% coverage)")
    print(f"  {'Lvl':>3}  {'Inclusive':>12}  {'Exclusive':>12}")
    print(f"  {'-' * 3}  {'-' * 12}  {'-' * 12}")
    for inc, exc in zip(report["inclusive"], report["exclusive"], strict=True):
        print(
            f"  {inc['level']:>3}  "
            f"{inc['known']:>4}/{inc['total']} ({inc['pct']:>5.1f}%)  "
            f"{exc['known']:>4}/{exc['total']} ({exc['pct']:>5.1f}%)"
        )
    nxt = report.get("next_level")
    if nxt:
        print(
            f"  Next band: Level {nxt['level']} — "
            f"{nxt['known']}/{nxt['total']} ({nxt['pct']}%), "
            f"{nxt['remaining']} words to go"
        )


def run(args: argparse.Namespace) -> int:
    if args.refresh_lists:
        ensure_hsk_lists(refresh=True)

    if not config.STATE_DB_PATH.exists():
        log.error("Local cache (%s) not initialised. Run `sync` first.",
                  config.STATE_DB_PATH.name)
        return 1

    with StateCache(config.STATE_DB_PATH) as cache:
        report = build_hsk_report_from_cache(
            cache,
            args.lang,
            refresh_lists=args.refresh_lists,
            threshold=args.threshold,
        )

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print(f"\nHSK coverage (lang={args.lang}, {report['known_word_count']} KNOWN words)")
    print(f"Lists: {report['lists_source']} (cached {report['lists_fetched_at']})")
    _print_standard(report["hsk20"])
    _print_standard(report["hsk30"])
    print()
    return 0

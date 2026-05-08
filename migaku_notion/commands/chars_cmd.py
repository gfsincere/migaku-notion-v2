"""`migaku-notion chars` — unique-Hanzi-character report from state.db.

Pure local, no Migaku API calls; ported verbatim from v1.
"""
from __future__ import annotations

import argparse
import logging

from .. import config
from ..state import StateCache


log = logging.getLogger("migaku-notion")


def _is_cjk(ch: str) -> bool:
    """True if `ch` is a CJK ideograph (Han character).

    Covers the main blocks where >99% of practical Mandarin chars live:
    CJK Unified Ideographs (U+4E00-U+9FFF), Extension A (U+3400-U+4DBF),
    and Extension B (U+20000-U+2A6DF).
    """
    if not ch:
        return False
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF
            or 0x3400 <= cp <= 0x4DBF
            or 0x20000 <= cp <= 0x2A6DF)


def run(args: argparse.Namespace) -> int:
    if not config.STATE_DB_PATH.exists():
        log.error("Local cache (%s) not initialised. Run `python -m migaku_notion sync` "
                  "or `python -m migaku_notion rebuild-cache` first.",
                  config.STATE_DB_PATH.name)
        return 1

    with StateCache(config.STATE_DB_PATH) as cache:
        rows = cache.load_all()

    by_status: dict[str, list[str]] = {}
    for row in rows.values():
        if row.lang != args.lang or row.archived:
            continue
        status = row.known_status or "(no status)"
        by_status.setdefault(status, []).append(row.dict_form)

    if not by_status:
        log.warning("No %s words in the local cache. Run `sync` first.", args.lang)
        return 1

    def chars_in(words: list[str]) -> set[str]:
        out: set[str] = set()
        for w in words:
            for ch in w:
                if _is_cjk(ch):
                    out.add(ch)
        return out

    print(f"\nUnique Hanzi character counts (lang={args.lang}, from local cache)")
    print("-" * 64)
    print(f"  {'Status':<14} {'Words':>8}  {'Unique chars':>14}")
    print(f"  {'-' * 14} {'-' * 8}  {'-' * 14}")
    cumulative_words = 0
    cumulative_chars: set[str] = set()
    for status in ("KNOWN", "LEARNING", "TRACKED", "UNKNOWN", "IGNORED"):
        words = by_status.get(status, [])
        if not words:
            continue
        chars = chars_in(words)
        print(f"  {status:<14} {len(words):>8}  {len(chars):>14}")
        cumulative_words += len(words)
        cumulative_chars |= chars

    known = chars_in(by_status.get("KNOWN", []))
    known_plus_learning = chars_in(by_status.get("KNOWN", []) + by_status.get("LEARNING", []))
    print()
    print(f"  KNOWN only             : {len(known):>5} unique chars")
    print(f"  KNOWN + LEARNING       : {len(known_plus_learning):>5} unique chars")
    print(f"  All statuses combined  : {len(cumulative_chars):>5} unique chars "
          f"(across {cumulative_words} words)")

    if args.list:
        print("\nKNOWN + LEARNING characters:")
        chars_sorted = sorted(known_plus_learning)
        for i in range(0, len(chars_sorted), 30):
            print("  " + "".join(chars_sorted[i:i + 30]))

    return 0

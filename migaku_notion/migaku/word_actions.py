"""Push word status changes to Migaku (/push/enqueue)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .. import config
from ..models import CachedRow
from ..state import StateCache
from . import auth, push


log = logging.getLogger("migaku-notion")

VALID_STATUSES = frozenset({"KNOWN", "LEARNING", "UNKNOWN", "IGNORED", "TRACKED"})


def find_cached_word_rows(
    cache: StateCache,
    lang: str,
    dict_form: str,
) -> list[CachedRow]:
    """All non-archived cache rows matching *dict_form* (any sense)."""
    form = dict_form.strip()
    if not form:
        return []
    rows = [
        r for r in cache.load_all().values()
        if r.lang == lang and not r.archived and (r.dict_form or "").strip() == form
    ]
    # Prefer LEARNING (actionable) over UNKNOWN/TRACKED.
    order = {"LEARNING": 0, "UNKNOWN": 1, "TRACKED": 2, "KNOWN": 3, "IGNORED": 4}
    rows.sort(key=lambda r: order.get((r.known_status or "UNKNOWN").upper(), 9))
    return rows


def _pick_push_fields(rows: list[CachedRow], lang: str) -> tuple[str, str]:
    if rows:
        row = rows[0]
        secondary = (row.sense_index or row.secondary or "0").strip() or "0"
        pos = (row.part_of_speech or "").strip()
        return secondary, pos
    # Words not yet in Migaku / cache — zh defaults from add-cards path.
    return "0", ""


def push_word_status(
    session: auth.AuthSession,
    cache: StateCache,
    *,
    dict_form: str,
    lang: str,
    status: str,
) -> dict[str, Any]:
    """Set a word's Migaku status and update local cache on success."""
    status_up = status.strip().upper()
    if status_up not in VALID_STATUSES:
        raise ValueError(f"Unsupported status: {status!r}")

    word = dict_form.strip()
    if not word:
        raise ValueError("word is required")

    device_id = config.get_or_create_device_id()
    device_version = max(cache.get_server_version(), 1)
    rows = find_cached_word_rows(cache, lang, word)
    secondary, pos = _pick_push_fields(rows, lang)

    log.info(
        "Pushing status %s for %s (lang=%s, secondary=%s, deviceVersion=%d)",
        status_up, word, lang, secondary, device_version,
    )
    result = push.set_word_status(
        session.token,
        device_id,
        device_version,
        word_text=word,
        secondary=secondary,
        part_of_speech=pos,
        language=lang,
        status=status_up,
    )

    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if rows:
        for row in rows:
            updated = CachedRow(
                migaku_key=row.migaku_key,
                page_id=row.page_id,
                lang=row.lang,
                dict_form=row.dict_form,
                secondary=row.secondary,
                known_status=status_up if status_up != "TRACKED" else "UNKNOWN",
                fail_rate=row.fail_rate,
                total_reviews=row.total_reviews,
                failed_reviews=row.failed_reviews,
                part_of_speech=row.part_of_speech,
                last_synced=now_iso,
                archived=row.archived,
                pinyin_marks=row.pinyin_marks,
                pinyin_numeric=row.pinyin_numeric,
                sense_index=row.sense_index,
                meaning=row.meaning,
                example=row.example,
                frequency_stars=row.frequency_stars,
                notion_meaning_was_blank=row.notion_meaning_was_blank,
            )
            cache.upsert(updated)
    else:
        log.info("No local cache row for %r — Migaku updated; run sync to pull the new word.", word)

    return {
        "ok": True,
        "word": word,
        "lang": lang,
        "status": status_up,
        "secondary": secondary,
        "cached_rows_updated": len(rows),
        "migaku": result,
    }

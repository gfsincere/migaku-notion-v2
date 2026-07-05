"""Push word status changes to Migaku (/push/enqueue)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .. import config
from ..models import CachedRow
from ..state import StateCache
from . import auth, push
from . import pull as pull_mod


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


def _upsert_cache_status(
    cache: StateCache,
    lang: str,
    word: str,
    status: str,
    *,
    secondary: str | None = None,
    meaning: str | None = None,
    pinyin_marks: str | None = None,
    pinyin_numeric: str | None = None,
    part_of_speech: str | None = None,
) -> int:
    """Update cached rows for *word* after a Migaku push (insert if new)."""
    status_up = status.strip().upper()
    rows = find_cached_word_rows(cache, lang, word)
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
                part_of_speech=part_of_speech or row.part_of_speech,
                last_synced=now_iso,
                archived=row.archived,
                pinyin_marks=pinyin_marks or row.pinyin_marks,
                pinyin_numeric=pinyin_numeric or row.pinyin_numeric,
                sense_index=row.sense_index,
                meaning=meaning or row.meaning,
                example=row.example,
                frequency_stars=row.frequency_stars,
                notion_meaning_was_blank=row.notion_meaning_was_blank,
            )
            cache.upsert(updated)
        return len(rows)

    sec = (secondary or "0").strip() or "0"
    migaku_key = f"{lang}|{word}|{sec}"
    cache.upsert(
        CachedRow(
            migaku_key=migaku_key,
            page_id=f"local:{migaku_key}",
            lang=lang,
            dict_form=word,
            secondary=sec,
            known_status=status_up if status_up != "TRACKED" else "UNKNOWN",
            fail_rate=None,
            total_reviews=None,
            failed_reviews=None,
            part_of_speech=part_of_speech,
            last_synced=now_iso,
            archived=False,
            pinyin_marks=pinyin_marks,
            pinyin_numeric=pinyin_numeric,
            sense_index=sec,
            meaning=meaning,
            example=None,
            frequency_stars=None,
            notion_meaning_was_blank=True,
        )
    )
    return 1


def bump_server_version_after_push(
    session: auth.AuthSession,
    cache: StateCache,
    push_result: dict[str, Any] | None,
) -> None:
    """Keep the pull-sync cursor current after dashboard pushes."""
    current = cache.get_server_version()
    candidate = current
    if isinstance(push_result, dict):
        received = push_result.get("receivedAt")
        if isinstance(received, int) and received > candidate:
            candidate = received
    device_id = config.get_or_create_device_id()
    try:
        payload = pull_mod.pull_sync(
            session,
            device_id,
            server_version=candidate,
            paginate=False,
            fallback_full_on_500=False,
        )
        candidate = max(candidate, pull_mod.next_server_version(payload, previous=candidate))
    except RuntimeError as exc:
        log.warning("Could not probe server version after push (%s); using receivedAt only.", exc)
    if candidate > current:
        cache.set_server_version(candidate)
        log.info("server_version: %d -> %d (after push)", current, candidate)


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

    cached_rows_updated = _upsert_cache_status(
        cache, lang, word, status_up, secondary=secondary,
    )

    bump_server_version_after_push(session, cache, result)

    return {
        "ok": True,
        "word": word,
        "lang": lang,
        "status": status_up,
        "secondary": secondary,
        "cached_rows_updated": cached_rows_updated,
        "migaku": result,
    }


def apply_word_action(
    session: auth.AuthSession,
    cache: StateCache,
    *,
    dict_form: str,
    lang: str,
    action: str,
) -> dict[str, Any]:
    """Dashboard / CLI: mark status or enqueue a dictionary-enriched card."""
    action_up = action.strip().upper()
    word = dict_form.strip()
    if not word:
        raise ValueError("word is required")

    if action_up in ("CREATE_CARD", "LEARNING"):
        from .card_create import enqueue_card_creation, load_card_create_context

        ctx = load_card_create_context(session, cache, lang)
        if action_up == "LEARNING" and word in ctx.words_with_cards:
            return push_word_status(
                session, cache, dict_form=word, lang=lang, status="LEARNING",
            )
        known_status = "UNKNOWN" if action_up == "CREATE_CARD" else "LEARNING"
        result = enqueue_card_creation(
            session,
            ctx,
            cache,
            dict_form=word,
            known_status=known_status,
        )
        if action_up == "LEARNING":
            _upsert_cache_status(
                cache,
                lang,
                word,
                "LEARNING",
                secondary=result.get("secondary"),
                meaning=result.get("meaning"),
                pinyin_marks=result.get("pinyin_marks"),
                pinyin_numeric=result.get("pinyin_numeric"),
                part_of_speech=result.get("part_of_speech"),
            )
        bump_server_version_after_push(session, cache, result.get("migaku"))
        return result

    return push_word_status(
        session,
        cache,
        dict_form=word,
        lang=lang,
        status=action_up,
    )

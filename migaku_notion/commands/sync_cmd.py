"""`migaku-notion sync` — pull from Migaku, enrich, sync into integration/cache.

Mirrors the structure of v1's `run_sync` (migaku-notion/sync/sync.py
lines 646-836) but:

  - Source: a single GET /pull-sync (instead of paginated migoku calls).
  - Server-version cursor: persisted in state.db's `meta` table; pass
    `--full-refresh` to ignore it and pull everything.
  - Enrichment: WordEnricher pulls pinyin / meaning / example /
    frequency from Migaku's published dict, with `pypinyin` only as a
    fallback for dict misses.
  - Difficulty: computed locally from /pull-sync's reviews + cards
    arrays (no separate /api/v1/words/difficult endpoint).
  - Meaning: auto-populated into rows whose integration-side Meaning is
    currently blank, but ONLY on the first v2 sync against a given
    state.db (gated by `meta.v2_first_sync_done`). Pass
    `--no-dict-meanings` to opt out entirely.

Notion is optional: if NOTION_* is unset (or --no-notion is passed), sync
still runs fully and updates local `state.db` only. That keeps the core pull +
enrichment + cache pipeline integration-agnostic and lets future sinks plug in.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from typing import Any

from .. import config
from ..migaku import auth, pull
from ..migaku.dict import MigakuDict
from ..migaku.enrichment import WordEnricher
from ..migaku.frequency import MigakuFrequency
from ..models import CachedRow, Word
from ..hanzi import add_cjk_chars
from ..notion_client import (
    NotionClient,
    build_database_totals_description,
    build_properties,
    cache_row_from_notion_page,
    format_parts_of_speech,
)
from ..state import StateCache, has_tracked_changes


log = logging.getLogger("migaku-notion")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cache_row_from_word(
    word: Word,
    *,
    page_id: str,
    last_synced: str | None,
    archived: bool = False,
    notion_meaning_was_blank: bool = True,
) -> CachedRow:
    sense_index = word.secondary if word.language == "zh" else None
    return CachedRow(
        migaku_key=word.key,
        page_id=page_id,
        lang=word.language,
        dict_form=word.dict_form,
        secondary=word.secondary,
        known_status=word.known_status or None,
        fail_rate=round(word.fail_rate, 2) if word.fail_rate is not None else None,
        total_reviews=word.total_reviews,
        failed_reviews=word.failed_reviews,
        part_of_speech=format_parts_of_speech(word.part_of_speech) or None,
        last_synced=last_synced,
        archived=archived,
        pinyin_marks=word.pinyin_marks or None,
        pinyin_numeric=word.pinyin_numeric or None,
        sense_index=sense_index,
        meaning=word.meaning or None,
        example=word.example or None,
        frequency_stars=word.frequency_stars,
        notion_meaning_was_blank=notion_meaning_was_blank,
    )


def _merge_difficulty(words: list[Word], difficult: list[dict[str, Any]]) -> None:
    """In-place: attach fail_rate / total_reviews / failed_reviews /
    parts_of_speech from compute_difficulty()'s output to Word objects.

    v1's merger also filled in part_of_speech from the difficulty
    endpoint when the word didn't already have one. v2 keeps that
    semantic — but with the relaxed key, since compute_difficulty's
    output is keyed by (dictForm, secondary).
    """
    by_key: dict[tuple[str, str], dict[str, Any]] = {
        (d["dictForm"], d["secondary"]): d for d in difficult
    }
    for w in words:
        match = by_key.get((w.dict_form, w.secondary))
        if not match:
            continue
        w.fail_rate = match.get("fail_rate")
        w.total_reviews = match.get("total_reviews")
        w.failed_reviews = match.get("failed_reviews")
        if not w.part_of_speech:
            pos_list = match.get("parts_of_speech") or []
            if pos_list:
                w.part_of_speech = ", ".join(sorted(set(pos_list)))


def _bootstrap_cache_from_notion(
    notion: NotionClient,
    cache: StateCache | None,
) -> dict[str, CachedRow]:
    pages = notion.query_all_pages()
    out: dict[str, CachedRow] = {}
    skipped = 0
    for page in pages:
        row = cache_row_from_notion_page(page)
        if row is None:
            skipped += 1
            continue
        if cache is not None:
            cache.upsert(row)
        out[row.migaku_key] = row
    log.info(
        "Bootstrap: %d Notion pages -> %d cached rows (%d skipped — no Migaku key)",
        len(pages), len(out), skipped,
    )
    return out


def _local_page_id(migaku_key: str) -> str:
    """Stable synthetic page id for local-only mode."""
    return f"local:{migaku_key}"


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:  # noqa: C901  (linear orchestration)
    notion_token = config.notion_token()
    notion_db = config.notion_database_id()
    notion_enabled = (not args.no_notion) and bool(notion_token and notion_db)
    if args.no_notion:
        log.info("Notion integration disabled via --no-notion; syncing to local cache only.")
    elif not notion_enabled:
        log.info("NOTION_TOKEN/NOTION_DATABASE_ID not set; running in local-only mode.")

    # --- Migaku auth ---------------------------------------------------
    try:
        session = auth.auth_session_from_env(
            refresh_token=config.migaku_refresh_token(),
            email=config.migaku_email(),
            password=config.migaku_password(),
        )
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    # Persist any rotated refresh token immediately so next run resumes cleanly.
    if session.refresh_token and session.refresh_token != (config.migaku_refresh_token() or ""):
        config.upsert_env_values({"MIGAKU_REFRESH_TOKEN": session.refresh_token})

    device_id = config.get_or_create_device_id()
    notion = NotionClient(notion_token, notion_db) if notion_enabled else None

    # --- Open cache (load + maybe bootstrap from Notion) ---------------
    cache: StateCache | None
    cache_rows: dict[str, CachedRow]
    if args.dry_run:
        # Dry run: read the cache if it exists but never write anywhere.
        cache = None
        if config.STATE_DB_PATH.exists():
            ro = StateCache(config.STATE_DB_PATH)
            cache_rows = ro.load_all()
            cache_server_version = ro.get_server_version()
            cache_first_sync_done = ro.is_v2_first_sync_done()
            ro.close()
            log.info(
                "Dry-run: loaded %d rows from local cache "
                "(server_version=%d, first_sync_done=%s)",
                len(cache_rows), cache_server_version, cache_first_sync_done,
            )
        else:
            if notion is not None:
                log.info("Dry-run: no state.db — bootstrapping in-memory from Notion ...")
                cache_rows = _bootstrap_cache_from_notion(notion, None)
            else:
                log.info("Dry-run: no state.db and no integration sink configured; using empty cache.")
                cache_rows = {}
            cache_server_version = 0
            cache_first_sync_done = False
    else:
        cache = StateCache(config.STATE_DB_PATH)
        cache_rows = cache.load_all()
        cache_server_version = cache.get_server_version()
        cache_first_sync_done = cache.is_v2_first_sync_done()
        if not cache_rows and notion is not None:
            log.info("Local cache empty — bootstrapping from Notion (one-time, "
                     "preserves existing rows) ...")
            cache_rows = _bootstrap_cache_from_notion(notion, cache)
        # Mirror the device id so future runs can sanity-check it matches .env.
        cache.set_device_id(device_id)

    # --- Pull from Migaku ---------------------------------------------
    sv = 0 if args.full_refresh else cache_server_version
    log.info("Fetching /pull-sync (lang=%s, serverVersion=%d, deviceId=%s...) ...",
             args.lang, sv, device_id[:8])
    try:
        payload = pull.pull_sync(session, device_id, server_version=sv)
    except RuntimeError as exc:
        log.error("%s", exc)
        if cache is not None:
            cache.close()
        return 1

    # Dedupe by key (Migaku's payload is supposed to be unique, but
    # defend against the same overlap bug we patched in migoku v1).
    raw_words = list(pull.words_from_payload(payload, args.lang))
    seen: set[str] = set()
    words: list[Word] = []
    for w in raw_words:
        if not w.dict_form:
            continue
        if w.key in seen:
            continue
        seen.add(w.key)
        words.append(w)

    if not words:
        log.warning("No words returned for lang=%s (serverVersion=%d). "
                    "Either there's nothing new since the last sync (try "
                    "`sync --full-refresh`) or your account has no %s words.",
                    args.lang, sv, args.lang)
        if cache is not None:
            cache.close()
        return 0
    log.info("Got %d unique words (after dedup from %d raw rows).",
             len(words), len(raw_words))

    # --- Filter by status ----------------------------------------------
    statuses = [s.strip().upper() for s in (args.status or "").split(",") if s.strip()]
    if statuses == ["ALL"]:
        statuses = []
    if statuses:
        keep = set(statuses)
        words = [w for w in words if (w.known_status or "").upper() in keep]
        log.info("After status filter (%s): %d words.", ",".join(statuses), len(words))

    # --- Difficulty (local aggregation) -------------------------------
    diff_limit = config.DEFAULT_DIFFICULT_LIMIT
    log.info("Computing fail-rate locally from /pull-sync (limit=%d) ...", diff_limit)
    try:
        difficult = pull.compute_difficulty(
            payload, language=args.lang, limit=diff_limit,
        )
        log.info("Got %d difficulty buckets (>= %d reviews).",
                 len(difficult), 5)
        _merge_difficulty(words, difficult)
    except Exception as exc:    # noqa: BLE001
        log.warning("Skipping difficulty enrichment: %s", exc)

    # --- Dictionary enrichment ----------------------------------------
    enricher: WordEnricher | None = None
    try:
        md = MigakuDict(args.lang)
        md.ensure_downloaded()
        mf = MigakuFrequency(args.lang)
        try:
            mf.ensure_downloaded()
        except Exception as exc:    # noqa: BLE001
            log.warning("Frequency DB unavailable (%s); proceeding without star ratings.",
                        exc)
            mf = None  # type: ignore[assignment]
        enricher = WordEnricher(dictionary=md, frequency=mf)
    except Exception as exc:    # noqa: BLE001
        log.warning("Dictionary unavailable (%s); falling back to pypinyin only.", exc)
        enricher = WordEnricher(dictionary=None, frequency=None)

    log.info("Enriching %d words from dict + frequency ...", len(words))
    for w in words:
        enricher.enrich(w)

    # --- Diff + upsert against integration sink -----------------------
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    do_meaning_writes = (not cache_first_sync_done) and (not args.no_dict_meanings)
    sink_name = "Notion" if notion is not None else "local cache"
    log.info(
        "Writing to %s (dry_run=%s, first_sync=%s, meaning_writes=%s) ...",
        sink_name, args.dry_run, not cache_first_sync_done, do_meaning_writes,
    )

    processed_words = processed_known = processed_learning = 0
    known_chars: set[str] = set()
    known_learning_chars: set[str] = set()
    if notion is not None and not args.dry_run and args.full_refresh:
        try:
            notion.remove_sync_totals_section()
            notion.update_database_description(
                build_database_totals_description(
                    total_words=0,
                    total_known=0,
                    total_learning=0,
                    unique_known_chars=0,
                    unique_known_learning_chars=0,
                )
            )
            log.info("Database-page sync totals enabled for this full refresh (updates every 100 rows).")
        except Exception as exc:  # noqa: BLE001
            log.warning("Couldn't initialise database-page sync totals: %s", exc)

    created = updated = unchanged = archived_count = 0
    seen_keys: set[str] = set()
    for i, word in enumerate(words, 1):
        seen_keys.add(word.key)
        cached = cache_rows.get(word.key)

        # Decide whether to include Meaning in this row's payload.
        # Cases (matches the pseudo-code in this module's docstring):
        #   - First v2 sync, --no-dict-meanings NOT set, AND row has a
        #     meaning to write, AND (brand-new OR cached row's
        #     notion_meaning_was_blank), AND we have a real meaning -> True
        #   - else False (matches v1's "Meaning is sacrosanct" rule).
        include_meaning = (
            do_meaning_writes
            and bool(word.meaning)
            and (cached is None or cached.notion_meaning_was_blank)
        )

        if cached is None:
            if args.dry_run:
                created += 1
            else:
                if notion is not None:
                    page = notion.create_page(
                        build_properties(word, include_meaning=include_meaning, now_iso=now_iso)
                    )
                    page_id = page.get("id")
                    if not page_id:
                        log.error("Notion create returned no page id for %s", word.key)
                        if cache is not None:
                            cache.close()
                        return 3
                else:
                    page_id = _local_page_id(word.key)
                # New rows: nothing was in Notion before, so by definition
                # the Meaning was blank. (We may have just written one.)
                row = _cache_row_from_word(
                    word, page_id=page_id, last_synced=now_iso,
                    notion_meaning_was_blank=not include_meaning,
                )
                cache.upsert(row)   # type: ignore[union-attr]
                cache_rows[word.key] = row
                created += 1

        elif cached.archived:
            if args.dry_run:
                updated += 1
            else:
                if notion is not None:
                    notion.update_page(
                        cached.page_id,
                        build_properties(word, include_meaning=include_meaning, now_iso=now_iso),
                        archived=False,
                    )
                row = _cache_row_from_word(
                    word, page_id=cached.page_id, last_synced=now_iso,
                    archived=False,
                    notion_meaning_was_blank=cached.notion_meaning_was_blank and not include_meaning,
                )
                cache.upsert(row)   # type: ignore[union-attr]
                cache_rows[word.key] = row
                updated += 1

        elif has_tracked_changes(word, cached) or include_meaning:
            if args.dry_run:
                updated += 1
            else:
                if notion is not None:
                    notion.update_page(
                        cached.page_id,
                        build_properties(word, include_meaning=include_meaning, now_iso=now_iso),
                    )
                row = _cache_row_from_word(
                    word, page_id=cached.page_id, last_synced=now_iso,
                    notion_meaning_was_blank=cached.notion_meaning_was_blank and not include_meaning,
                )
                cache.upsert(row)   # type: ignore[union-attr]
                cache_rows[word.key] = row
                updated += 1

        else:
            unchanged += 1

        processed_words += 1
        status_upper = (word.known_status or "").upper()
        if status_upper == "KNOWN":
            processed_known += 1
            add_cjk_chars(word.dict_form, known_chars)
            add_cjk_chars(word.dict_form, known_learning_chars)
        elif status_upper == "LEARNING":
            processed_learning += 1
            add_cjk_chars(word.dict_form, known_learning_chars)

        if notion is not None and not args.dry_run and args.full_refresh and (i % 100 == 0 or i == len(words)):
            try:
                notion.update_database_description(
                    build_database_totals_description(
                        total_words=processed_words,
                        total_known=processed_known,
                        total_learning=processed_learning,
                        unique_known_chars=len(known_chars),
                        unique_known_learning_chars=len(known_learning_chars),
                    )
                )
                log.info(
                    "  ... database-page totals updated (words=%d known=%d learning=%d "
                    "chars_known=%d chars_known+learning=%d)",
                    processed_words,
                    processed_known,
                    processed_learning,
                    len(known_chars),
                    len(known_learning_chars),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Couldn't update database-page sync totals: %s", exc)

        if i % 200 == 0:
            log.info("  ... %d/%d processed (created=%d updated=%d unchanged=%d)",
                     i, len(words), created, updated, unchanged)

    # --- Archive stale rows -------------------------------------------
    if args.archive_stale:
        stale = [r for r in cache_rows.values()
                 if r.migaku_key not in seen_keys
                 and r.lang == args.lang
                 and not r.archived]
        log.info("Archiving %d stale rows (no longer in Migaku for lang=%s).",
                 len(stale), args.lang)
        for r in stale:
            if args.dry_run:
                archived_count += 1
                continue
            if notion is not None and not r.page_id.startswith("local:"):
                notion.archive_page(r.page_id)
            cache.mark_archived(r.migaku_key, archived=True, last_synced=now_iso)   # type: ignore[union-attr]
            archived_count += 1

    # --- Persist resume cursors + first-sync flag ---------------------
    if cache is not None:
        new_sv = pull.next_server_version(payload, previous=cache_server_version)
        if new_sv > cache_server_version:
            cache.set_server_version(new_sv)
            log.info("server_version: %d -> %d", cache_server_version, new_sv)
        if args.full_refresh:
            cache.mark_full_pull(now_iso)
        if not cache_first_sync_done:
            cache.mark_v2_first_sync_done()
            log.info("Flagged v2_first_sync_done=1; future syncs will not "
                     "auto-populate Meaning.")
        snapshot_date = now_iso[:10]
        snap = cache.record_progress_snapshot(args.lang, snapshot_date)
        log.info(
            "Progress snapshot %s: known_words=%d known_chars=%d",
            snapshot_date, snap.known_words, snap.known_chars,
        )
        cache.close()

    log.info(
        "Done. created=%d updated=%d unchanged=%d archived=%d (dry_run=%s)",
        created, updated, unchanged, archived_count, args.dry_run,
    )
    return 0


def run_full_refresh(lang: str | None = None) -> int:
    """Full Migaku pull + cache (+ optional Notion). Used by the dashboard."""
    args = argparse.Namespace(
        lang=lang or config.DEFAULT_LANG,
        status=config.DEFAULT_STATUS,
        dry_run=False,
        archive_stale=False,
        full_refresh=True,
        no_dict_meanings=False,
        no_notion=False,
    )
    return run(args)

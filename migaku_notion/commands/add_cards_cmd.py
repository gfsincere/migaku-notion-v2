"""`migaku-notion add-cards` — enqueue card creation from a Notion word list.

This is a pragmatic first pass:
  - Source rows come from a user-specified Notion database.
  - Dedupe runs against local state.db, target Notion DB, and Migaku words/cards.
  - Execution enqueues word records in TRACKED state (Migaku-side create flow).
"""
from __future__ import annotations

import argparse
import logging
import time
from typing import Any

from .. import config
from ..migaku import auth, pull, push
from ..migaku.dict import MigakuDict
from ..migaku.enrichment import WordEnricher
from ..models import Word
from ..notion_client import NotionClient
from ..pinyin import compute_pinyin_numeric
from ..state import StateCache

log = logging.getLogger("migaku-notion")


def _extract_title(properties: dict[str, Any], preferred_prop: str) -> str:
    prop = properties.get(preferred_prop)
    if isinstance(prop, dict) and prop.get("type") == "title":
        title_arr = prop.get("title") or []
        return "".join(seg.get("plain_text", "") for seg in title_arr).strip()

    for p in properties.values():
        if isinstance(p, dict) and p.get("type") == "title":
            title_arr = p.get("title") or []
            text = "".join(seg.get("plain_text", "") for seg in title_arr).strip()
            if text:
                return text
    return ""


def _extract_rich_text(properties: dict[str, Any], prop_name: str) -> str:
    prop = properties.get(prop_name)
    if not isinstance(prop, dict):
        return ""
    ptype = prop.get("type")
    if ptype == "rich_text":
        return "".join(seg.get("plain_text", "") for seg in (prop.get("rich_text") or [])).strip()
    if ptype == "title":
        return "".join(seg.get("plain_text", "") for seg in (prop.get("title") or [])).strip()
    if ptype == "select":
        sel = prop.get("select") or {}
        return str(sel.get("name") or "").strip()
    return ""


def _plain_text_from_rich(rich: list[dict[str, Any]]) -> str:
    return "".join(seg.get("plain_text", "") for seg in (rich or [])).strip()


def _parse_word_line(line: str) -> dict[str, str] | None:
    parts = [p.strip() for p in line.split("|")]
    if not parts or not parts[0]:
        return None
    word = parts[0]
    secondary = parts[1] if len(parts) >= 2 and parts[1] else "0"
    pos = parts[2] if len(parts) >= 3 else ""
    return {"word": word, "secondary": secondary, "part_of_speech": pos}


def _extract_page_rows(notion: NotionClient, page_id: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    queue: list[tuple[str, int]] = [(page_id, 0)]
    seen: set[str] = set()
    while queue:
        parent_id, depth = queue.pop(0)
        if parent_id in seen:
            continue
        seen.add(parent_id)
        blocks = notion.list_block_children(parent_id)
        for block in blocks:
            block_id = str(block.get("id") or "")
            has_children = bool(block.get("has_children"))
            btype = block.get("type")
            if not isinstance(btype, str):
                if has_children and block_id and depth < 8:
                    queue.append((block_id, depth + 1))
                continue

            # Structured table rows: each row has cells = list[list[rich_text]]
            if btype == "table_row":
                payload = block.get("table_row") or {}
                for cell in (payload.get("cells") or []):
                    line = _plain_text_from_rich(cell or [])
                    parsed = _parse_word_line(line)
                    if parsed:
                        rows.append(parsed)
                if has_children and block_id and depth < 8:
                    queue.append((block_id, depth + 1))
                continue

            payload = block.get(btype) or {}
            rich = payload.get("rich_text") or []
            line = _plain_text_from_rich(rich)
            parsed = _parse_word_line(line) if line else None
            if parsed:
                rows.append(parsed)

            # Recurse into toggles, list items, synced blocks, columns, etc.
            if has_children and block_id and depth < 8:
                queue.append((block_id, depth + 1))
    return rows


def run(args: argparse.Namespace) -> int:
    token = config.notion_token()
    if not token:
        log.error("NOTION_TOKEN is required for add-cards.")
        return 2
    inline_words = [w.strip() for w in (args.words or "").split(",") if w.strip()]
    source_db = (args.source_db or "").strip()
    source_page = (args.source_page or "").strip()
    if not source_db and not source_page and not inline_words:
        log.error("Pass --source-db, --source-page, or --words.")
        return 2

    target_db = (args.target_db or config.notion_database_id() or "").strip()
    if not target_db:
        log.error("Target Notion DB is required (set NOTION_DATABASE_ID or pass --target-db).")
        return 2

    target_notion = NotionClient(token, target_db)

    source_rows: list[dict[str, str]] = []
    if inline_words:
        source_rows.extend(
            {"word": w, "secondary": "0", "part_of_speech": ""} for w in inline_words
        )
    elif source_db:
        source_notion = NotionClient(token, source_db)
        source_pages = source_notion.query_all_pages()
        if not source_pages:
            log.warning("No rows found in source database.")
            return 0
        for page in source_pages:
            props = page.get("properties") or {}
            word = _extract_title(props, args.source_word_prop).strip()
            if not word:
                continue
            source_rows.append(
                {
                    "word": word,
                    "secondary": _extract_rich_text(props, args.source_secondary_prop) or "0",
                    "part_of_speech": _extract_rich_text(props, args.source_pos_prop),
                }
            )
    else:
        source_rows = _extract_page_rows(target_notion, source_page)
        if not source_rows:
            log.warning("No usable rows found in source page.")
            return 0

    if not source_rows:
        log.warning("No usable source rows (missing title/word values).")
        return 0

    words_ordered: list[dict[str, str]] = []
    seen = set()
    for row in source_rows:
        if row["word"] in seen:
            continue
        seen.add(row["word"])
        words_ordered.append(row)

    with StateCache(config.STATE_DB_PATH) as cache:
        cache_rows = cache.load_all() if config.STATE_DB_PATH.exists() else {}
        server_version = cache.get_server_version()

    local_words = {r.dict_form for r in cache_rows.values() if r.lang == args.lang and not r.archived}
    target_words = {
        _extract_title((p.get("properties") or {}), "Word")
        for p in target_notion.query_all_pages()
    }
    target_words.discard("")

    session = auth.auth_session_from_env(
        refresh_token=config.migaku_refresh_token(),
        email=config.migaku_email(),
        password=config.migaku_password(),
    )
    device_id = config.get_or_create_device_id()
    migaku_payload = pull.pull_sync(session, device_id, server_version=0)
    cards_payload = [c for c in (migaku_payload.get("cards") or []) if isinstance(c, dict) and not c.get("del")]
    relations_payload = [r for r in (migaku_payload.get("cardWordRelations") or []) if isinstance(r, dict) and not r.get("del")]
    words_payload = [w for w in (migaku_payload.get("words") or []) if isinstance(w, dict) and not w.get("del")]
    if not cards_payload or not relations_payload:
        log.error("Migaku payload missing cards/relations; cannot build card template.")
        return 2

    zh_fresh_cards = [
        c for c in cards_payload
        if c.get("reviewCount") in (0, None)
    ]
    template_card = max(zh_fresh_cards or cards_payload, key=lambda c: int(c.get("mod") or 0))
    template_relation = next(
        (r for r in relations_payload if str(r.get("language") or "") == args.lang),
        relations_payload[0],
    )
    max_card_id = max(int(c.get("id") or 0) for c in cards_payload)

    dict_client = None
    enricher = None
    try:
        dict_client = MigakuDict(args.lang)
        dict_client.ensure_downloaded()
        enricher = WordEnricher(dictionary=dict_client, frequency=None)
    except Exception:
        enricher = None
    migaku_words = set()
    migaku_words_with_cards = set()
    for w in migaku_payload.get("words") or []:
        if not isinstance(w, dict) or w.get("del"):
            continue
        if (w.get("language") or "") not in ("", args.lang):
            continue
        df = str(w.get("dictForm") or "").strip()
        if not df:
            continue
        migaku_words.add(df)
        if w.get("hasCard"):
            migaku_words_with_cards.add(df)

    candidates: list[dict[str, str]] = []
    skipped_local = skipped_notion = skipped_migaku_card = 0
    for row in words_ordered:
        w = row["word"]
        if w in local_words:
            skipped_local += 1
            continue
        if w in target_words:
            skipped_notion += 1
            continue
        if w in migaku_words_with_cards:
            skipped_migaku_card += 1
            continue
        candidates.append(row)

    if args.limit and args.limit > 0:
        candidates = candidates[: args.limit]

    log.info(
        "Source=%d unique=%d candidates=%d skipped(local=%d notion=%d migaku_has_card=%d)",
        len(source_rows),
        len(words_ordered),
        len(candidates),
        skipped_local,
        skipped_notion,
        skipped_migaku_card,
    )
    if not candidates:
        return 0

    if not args.apply:
        log.info("Dry run only. Re-run with --apply to enqueue card creation.")
        for row in candidates[: min(20, len(candidates))]:
            log.info("  candidate: %s", row["word"])
        if len(candidates) > 20:
            log.info("  ... and %d more", len(candidates) - 20)
        return 0

    device_version = max(server_version, pull.next_server_version(migaku_payload, previous=server_version))
    created = 0
    next_card_id = max(int(time.time() * 1000), max_card_id + 1)
    for row in candidates:
        now_ms = int(time.time() * 1000)
        card_id = next_card_id
        next_card_id += 1
        base_word = Word(
            dict_form=row["word"],
            secondary=row["secondary"] or "0",
            known_status="UNKNOWN",
            language=args.lang,
            part_of_speech=row["part_of_speech"] or None,
        )
        if enricher is not None:
            try:
                enricher.enrich(base_word)
            except Exception:
                pass
        pos = (base_word.part_of_speech or row["part_of_speech"] or "x").split(",")[0].strip() or "x"
        pinyin_num = (base_word.pinyin_numeric or compute_pinyin_numeric(row["word"]) or "").strip()
        pinyin_marks = (base_word.pinyin_marks or "").strip()
        reading = pinyin_num or pinyin_marks or "?"
        primary = f"{row['word']}[{reading};{pos}]"
        secondary = f"<t>{row['word']}[{reading};{pos}]</t>"
        meaning = (base_word.meaning or "").strip() or row["word"]
        example = (base_word.example or "").strip()
        meaning_block = meaning
        if pinyin_marks:
            meaning_block += f"\x1f<p>{row['word']} ({pinyin_marks})</p>"
        if example:
            meaning_block += f"<p>{example}</p>"
        fields = (
            f"{meaning_block}\x1f\x1fAuto-created by migaku-notion-v2."
            "\x1f{\"syntax\":{\"targetWordEdited\":false,\"sentenceEdited\":false}}"
        )
        card_words = f"\x1f{row['word']}|{row['secondary'] or '0'}|{pos}\x1f"

        word_record = {
            "dictForm": row["word"],
            "secondary": row["secondary"] or "0",
            "partOfSpeech": pos,
            "language": args.lang,
            "mod": now_ms,
            "serverMod": -1,
            "del": 0,
            "knownStatus": "UNKNOWN",
            "hasCard": True,
            "tracked": True,
            "created": now_ms,
            "isModern": True,
            "serverVersion": int(template_card.get("serverVersion") or 0),
            "isPendingEnqueue": True,
            "isPendingApply": False,
        }
        card_record = {
            **template_card,
            "id": card_id,
            "mod": now_ms,
            "serverMod": -1,
            "del": 0,
            "seedMod": 0,
            "seedDel": 0,
            "isSeed": False,
            "created": now_ms,
            "primaryField": primary,
            "secondaryField": secondary,
            "fields": fields,
            "words": card_words,
            "reviewCount": 0,
            "passCount": 0,
            "failCount": 0,
            "lapseCount": 0,
            "lastReview": 0,
            "notes": "",
            "suspended": False,
            "isSample": False,
            "replacementCardId": 0,
            "isPendingEnqueue": True,
            "isPendingApply": False,
        }
        relation_record = {
            **template_relation,
            "mod": now_ms + 1,
            "serverMod": -1,
            "del": 0,
            "seedMod": 0,
            "seedDel": 0,
            "isSeed": False,
            "cardId": card_id,
            "dictForm": row["word"],
            "secondary": row["secondary"] or "0",
            "partOfSpeech": pos,
            "language": args.lang,
            "isTargetWord": True,
            "occurrences": 1,
            "isPendingEnqueue": True,
            "isPendingApply": False,
        }
        payload = push.empty_payload()
        payload["words"] = [word_record]
        payload["cards"] = [card_record]
        payload["cardWordRelations"] = [relation_record]
        push.push_enqueue(
            session.token,
            device_id,
            device_version,
            payload,
        )
        created += 1
        if args.smoke_tests and created >= args.smoke_tests:
            break

    log.info("Enqueued %d new word(s) for card creation flow.", created)
    return 0


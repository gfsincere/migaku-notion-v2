"""Enqueue Migaku card creation (/push/enqueue with words + cards + relations).

Reverse-engineered from manual card-create HAR captures. Enriches from
Migaku's published dictionary (meaning, example, pinyin) before push.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .. import config
from ..models import Word
from ..pinyin import compute_pinyin_numeric
from ..state import StateCache
from . import auth, pull, push
from .dict import MigakuDict
from .enrichment import WordEnricher
from .word_actions import find_cached_word_rows


log = logging.getLogger("migaku-notion")


@dataclass
class CardCreateContext:
    """Templates and counters for one dashboard / add-cards session."""

    device_id: str
    device_version: int
    lang: str
    template_card: dict[str, Any]
    template_relation: dict[str, Any]
    enricher: WordEnricher | None
    words_with_cards: set[str] = field(default_factory=set)
    next_card_id: int = 0


def load_card_create_context(
    session: auth.AuthSession,
    cache: StateCache,
    lang: str,
) -> CardCreateContext:
    """Pull Migaku once and build card-creation templates."""
    device_id = config.get_or_create_device_id()
    server_version = cache.get_server_version()
    migaku_payload = pull.pull_sync(session, device_id, server_version=0)

    cards_payload = [
        c for c in (migaku_payload.get("cards") or [])
        if isinstance(c, dict) and not c.get("del")
    ]
    relations_payload = [
        r for r in (migaku_payload.get("cardWordRelations") or [])
        if isinstance(r, dict) and not r.get("del")
    ]
    if not cards_payload or not relations_payload:
        raise RuntimeError(
            "Migaku payload missing cards/relations; cannot build card template."
        )

    zh_fresh = [c for c in cards_payload if c.get("reviewCount") in (0, None)]
    template_card = max(zh_fresh or cards_payload, key=lambda c: int(c.get("mod") or 0))
    template_relation = next(
        (r for r in relations_payload if str(r.get("language") or "") == lang),
        relations_payload[0],
    )
    max_card_id = max(int(c.get("id") or 0) for c in cards_payload)

    enricher: WordEnricher | None = None
    try:
        md = MigakuDict(lang)
        md.ensure_downloaded()
        enricher = WordEnricher(dictionary=md, frequency=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("Dictionary unavailable for card create (%s); pypinyin fallback only.", exc)

    words_with_cards: set[str] = set()
    for w in migaku_payload.get("words") or []:
        if not isinstance(w, dict) or w.get("del"):
            continue
        if (w.get("language") or "") not in ("", lang):
            continue
        df = str(w.get("dictForm") or "").strip()
        if df and w.get("hasCard"):
            words_with_cards.add(df)

    device_version = max(server_version, pull.next_server_version(migaku_payload, previous=server_version))
    next_card_id = max(int(time.time() * 1000), max_card_id + 1)

    return CardCreateContext(
        device_id=device_id,
        device_version=device_version,
        lang=lang,
        template_card=template_card,
        template_relation=template_relation,
        enricher=enricher,
        words_with_cards=words_with_cards,
        next_card_id=next_card_id,
    )


def _build_enqueue_payload(
    *,
    row: dict[str, str],
    lang: str,
    template_card: dict[str, Any],
    template_relation: dict[str, Any],
    card_id: int,
    enricher: WordEnricher | None,
    now_ms: int,
    known_status: str = "UNKNOWN",
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return (push payload, summary fields for UI)."""
    base_word = Word(
        dict_form=row["word"],
        secondary=row.get("secondary") or "0",
        known_status="UNKNOWN",
        language=lang,
        part_of_speech=row.get("part_of_speech") or None,
    )
    if enricher is not None:
        try:
            enricher.enrich(base_word)
        except Exception:  # noqa: BLE001
            pass

    pos = (base_word.part_of_speech or row.get("part_of_speech") or "x").split(",")[0].strip() or "x"
    pinyin_num = (base_word.pinyin_numeric or compute_pinyin_numeric(row["word"]) or "").strip()
    pinyin_marks = (base_word.pinyin_marks or "").strip()
    reading = pinyin_num or pinyin_marks or "?"
    primary = f"{row['word']}[{reading};{pos}]"
    secondary_field = f"<t>{row['word']}[{reading};{pos}]</t>"
    meaning = (base_word.meaning or "").strip() or row["word"]
    example = (base_word.example or "").strip()
    meaning_block = meaning
    if pinyin_marks:
        meaning_block += f"\x1f<p>{row['word']} ({pinyin_marks})</p>"
    if example:
        meaning_block += f"<p>{example}</p>"
    fields = (
        f"{meaning_block}\x1f\x1fAuto-created by migaku-notion-v2."
        '\x1f{"syntax":{"targetWordEdited":false,"sentenceEdited":false}}'
    )
    card_words = f"\x1f{row['word']}|{row.get('secondary') or '0'}|{pos}\x1f"

    word_record = {
        "dictForm": row["word"],
        "secondary": row.get("secondary") or "0",
        "partOfSpeech": pos,
        "language": lang,
        "mod": now_ms,
        "serverMod": -1,
        "del": 0,
        "knownStatus": known_status.strip().upper() or "UNKNOWN",
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
        "secondaryField": secondary_field,
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
        "secondary": row.get("secondary") or "0",
        "partOfSpeech": pos,
        "language": lang,
        "isTargetWord": True,
        "occurrences": 1,
        "isPendingEnqueue": True,
        "isPendingApply": False,
    }

    payload = push.empty_payload()
    payload["words"] = [word_record]
    payload["cards"] = [card_record]
    payload["cardWordRelations"] = [relation_record]

    summary = {
        "meaning": meaning,
        "pinyin_marks": pinyin_marks,
        "pinyin_numeric": pinyin_num,
        "part_of_speech": pos,
        "example": example,
    }
    return payload, summary


def enqueue_card_creation(
    session: auth.AuthSession,
    ctx: CardCreateContext,
    cache: StateCache,
    *,
    dict_form: str,
    secondary: str | None = None,
    part_of_speech: str | None = None,
    known_status: str = "UNKNOWN",
) -> dict[str, Any]:
    """Create one dictionary-enriched card in Migaku."""
    word = dict_form.strip()
    if not word:
        raise ValueError("word is required")
    if word in ctx.words_with_cards:
        raise RuntimeError(f"{word} already has a card in Migaku")

    cached = find_cached_word_rows(cache, ctx.lang, word)
    if secondary is None:
        secondary = (
            (cached[0].sense_index or cached[0].secondary or "0").strip() if cached else "0"
        )
    if part_of_speech is None:
        part_of_speech = (cached[0].part_of_speech or "").strip() if cached else ""

    row = {
        "word": word,
        "secondary": secondary or "0",
        "part_of_speech": part_of_speech or "",
    }
    now_ms = int(time.time() * 1000)
    card_id = ctx.next_card_id
    ctx.next_card_id += 1

    status_up = known_status.strip().upper() or "UNKNOWN"

    payload, summary = _build_enqueue_payload(
        row=row,
        lang=ctx.lang,
        template_card=ctx.template_card,
        template_relation=ctx.template_relation,
        card_id=card_id,
        enricher=ctx.enricher,
        now_ms=now_ms,
        known_status=status_up,
    )

    log.info(
        "Enqueuing card for %s (lang=%s, card_id=%d, status=%s)",
        word, ctx.lang, card_id, status_up,
    )
    result = push.push_enqueue(
        session.token,
        ctx.device_id,
        ctx.device_version,
        payload,
    )
    ctx.words_with_cards.add(word)

    return {
        "ok": True,
        "action": "CREATE_CARD" if status_up == "UNKNOWN" else status_up,
        "word": word,
        "lang": ctx.lang,
        "status": status_up,
        "secondary": secondary or "0",
        "card_id": card_id,
        "migaku": result,
        **summary,
    }

"""Core dataclasses shared across the v2 package.

`Word` and `CachedRow` started life as a verbatim port of v1's, with the
v1-shape `migaku_key` (`"<lang>|<dictForm>|<secondary>"`) preserved so
users can copy a v1 `state.db` across without rebuild.

v2 additions on both:
  - `meaning`            — Migaku-published dictionary definition.
  - `example`            — first example sentence from the dict (or the
                           user's own card if they have one).
  - `frequency_stars`    — 1-5 quintile based on Migaku's frequency DB,
                           matching the star rating shown in the
                           extension UI.

`CachedRow` additionally carries `notion_meaning_was_blank` — captured
once at bootstrap so the first-v2-sync auto-populate path knows whether
a row's Meaning column is safe to write to. After that flag has been
acted on (and `meta.v2_first_sync_done` flips to true), it stops
mattering.

`MigakuEntity` wraps the wire-shape of a single record from /pull-sync.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Word:
    """The shape `sync` operates on (after enrichment)."""

    dict_form: str
    secondary: str
    known_status: str
    language: str
    fail_rate: float | None = None
    total_reviews: int | None = None
    failed_reviews: int | None = None
    part_of_speech: str | None = None
    pinyin_marks: str | None = None
    pinyin_numeric: str | None = None

    # v2 additions, populated by migaku_notion.migaku.enrichment.enrich().
    meaning: str | None = None
    example: str | None = None
    frequency_stars: int | None = None

    @property
    def key(self) -> str:
        return f"{self.language}|{self.dict_form}|{self.secondary}"


@dataclass
class CachedRow:
    """One row in state.db. Mirrors the `words` SQLite table 1:1.

    The first 15 fields are the v1 schema verbatim. Anything below is
    v2-only and migrated in idempotently — see `state.StateCache.__init__`
    for the ALTER TABLE shims.
    """

    migaku_key: str
    page_id: str
    lang: str
    dict_form: str
    secondary: str
    known_status: str | None
    fail_rate: float | None
    total_reviews: int | None
    failed_reviews: int | None
    part_of_speech: str | None
    last_synced: str | None
    archived: bool
    pinyin_marks: str | None = None
    pinyin_numeric: str | None = None
    sense_index: str | None = None

    # v2 additions.
    meaning: str | None = None
    example: str | None = None
    frequency_stars: int | None = None
    # True iff, at the moment we first observed this Notion page, its
    # Meaning column was empty. Used to decide whether to auto-populate
    # Meaning on the first v2 sync (Greg, 2026-05-07: only fill blanks,
    # never overwrite). After that first sync runs, stays as a record
    # of the original state but is no longer consulted.
    notion_meaning_was_blank: bool = True


@dataclass
class MigakuEntity:
    """Raw record from `/pull-sync` (for any of the entity arrays).

    /pull-sync returns 14 parallel arrays:
        decks, cardTypes, cards, cardWordRelations, vacations, reviews,
        words, config, keyValue, learningMaterials, lessons, reviewHistory,
        wordHistory, libraryItems

    For the read-side of the v2 sync we mostly care about `words`. The
    other arrays are kept around as opaque dicts in case we need to
    round-trip them (e.g. when pushing back via /push/enqueue, the body
    payload is a *full* `migakuSyncPayload` with every array present, even
    if most are empty — see migoku/migaku_api.go::PushSync).
    """

    kind: str           # "words", "cards", "decks", ...
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def word_from_raw(cls, row: dict[str, Any], language: str) -> Word:
        """Project a raw `words[]` row from /pull-sync into a `Word`."""
        return Word(
            dict_form=row.get("dictForm", "") or "",
            secondary=row.get("secondary", "") or "",
            known_status=row.get("knownStatus") or "UNKNOWN",
            language=row.get("language") or language,
            part_of_speech=row.get("partOfSpeech") or None,
        )

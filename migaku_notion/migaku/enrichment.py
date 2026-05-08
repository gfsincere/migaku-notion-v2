"""Per-Word enrichment: dict lookup → frequency → pypinyin fallback.

Sits between `migaku.pull.list_words()` (which returns Words with only
the fields Migaku itself stores: dict_form, secondary, status, language,
partOfSpeech) and the Notion write step (which wants pinyin, meaning,
example sentence, frequency stars).

Design (Greg, 2026-05-07):
  - **Dict is authoritative.** Pinyin / meaning / examples come from
    Migaku's published dict whenever it has the word. This matches what
    the Migaku extension itself shows the user, including correct
    homonym disambiguation via `secondary` (`0`/`1`/...).
  - **Frequency: prefer the dict's own field.** If the dict carries an
    inline `frequencyStars`, use it. Else fall back to the per-language
    frequency DB and bucket the raw rank into quintiles ourselves.
  - **`pypinyin` is a fallback only.** When the dict doesn't have the
    word (rare for course vocab; common for proper nouns / made-up
    words), generate pinyin client-side same as v1. Log at INFO so users
    can see the gap.
  - **Meaning special case** — see `migaku.commands.sync_cmd` for the
    "first v2 sync only populates blanks" guard. The enricher always
    fills `word.meaning` if the dict has it; the *sync* code decides
    whether to actually include it in the Notion update payload.

Usage (once everything else is wired):

    from migaku_notion.migaku import enrichment, dict as dict_mod, frequency
    enricher = enrichment.WordEnricher(
        dictionary=dict_mod.MigakuDict("zh_CN", target_lang="en"),
        frequency=frequency.MigakuFrequency("zh_CN"),
        use_pypinyin_fallback=True,
    )
    for word in words:
        enricher.enrich(word)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..pinyin import (
    PINYIN_AVAILABLE,
    compute_pinyin_marks,
    compute_pinyin_numeric,
)


if TYPE_CHECKING:
    from ..models import Word
    from .dict import MigakuDict
    from .frequency import MigakuFrequency


log = logging.getLogger("migaku-notion")


class WordEnricher:
    """Stateful enrichment pipeline. Reusable across the whole sync.

    Holds onto the (lazily-bootstrapped) dict + frequency clients so
    each lookup amortises the SQLite connection setup.
    """

    def __init__(
        self,
        *,
        dictionary: "MigakuDict | None" = None,
        frequency: "MigakuFrequency | None" = None,
        use_pypinyin_fallback: bool = True,
    ) -> None:
        self.dictionary = dictionary
        self.frequency = frequency
        self.use_pypinyin_fallback = use_pypinyin_fallback
        self._misses_logged = 0

    def enrich(self, word: "Word") -> None:
        """Mutate `word` in place. Idempotent (safe to call twice)."""
        entry = None
        if self.dictionary is not None:
            try:
                entry = self.dictionary.lookup(
                    word.dict_form, sense_index=word.secondary or 0
                )
            except NotImplementedError:
                # Stub mode — the next session wires the SQL. Until then
                # the enricher silently degrades to pypinyin-only.
                entry = None

        if entry is not None:
            # Dict hit: prefer dict over anything we'd derive locally.
            if entry.reading and not word.pinyin_marks:
                word.pinyin_marks = entry.reading
            if entry.meaning and not word.meaning:
                word.meaning = entry.meaning
            if entry.examples and not word.example:
                word.example = entry.examples[0]
            if entry.parts_of_speech and not word.part_of_speech:
                # Comma-join here so the Word's str-typed
                # part_of_speech field stays valid; build_properties
                # will normalise it again on the way out.
                word.part_of_speech = ", ".join(sorted(set(entry.parts_of_speech)))
            stars = entry.frequency_stars
            if stars is None and self.frequency is not None:
                try:
                    stars = self.frequency.stars(word.dict_form)
                except NotImplementedError:
                    stars = None
            if stars is not None:
                word.frequency_stars = stars
        else:
            # Dict miss: log (sampled) + fall back to pypinyin if zh.
            self._log_miss(word)
            if self.use_pypinyin_fallback and word.language == "zh":
                if PINYIN_AVAILABLE:
                    if not word.pinyin_marks:
                        word.pinyin_marks = compute_pinyin_marks(word.dict_form)
                    if not word.pinyin_numeric:
                        word.pinyin_numeric = compute_pinyin_numeric(word.dict_form)
                # Frequency: still try the standalone frequency DB.
                if self.frequency is not None and word.frequency_stars is None:
                    try:
                        word.frequency_stars = self.frequency.stars(word.dict_form)
                    except NotImplementedError:
                        pass

        # Always populate numeric pinyin too for zh (Notion has a
        # dedicated column). If the dict gave us tone-marks but not
        # numeric, derive numeric via pypinyin from the dict-supplied
        # marks string isn't trivial, so we fall back to deriving
        # numeric from the Hanzi directly. This rarely diverges from
        # the dict's marks because pypinyin matches Migaku for the
        # default reading.
        if word.language == "zh" and not word.pinyin_numeric and PINYIN_AVAILABLE:
            word.pinyin_numeric = compute_pinyin_numeric(word.dict_form)

    def _log_miss(self, word: "Word") -> None:
        # Don't drown the log if a user has thousands of dict misses;
        # log the first 25 individually, then summarise per-100 chunks.
        self._misses_logged += 1
        if self._misses_logged <= 25:
            log.info("dict miss for %s|%s|%s — using pypinyin fallback "
                     "(meaning/example will be blank)",
                     word.language, word.dict_form, word.secondary or "0")
        elif self._misses_logged % 100 == 0:
            log.info("dict misses so far: %d (suppressing per-row logs)",
                     self._misses_logged)

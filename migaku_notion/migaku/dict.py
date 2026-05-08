"""Client for Migaku's published dictionary databases.

Migaku ships a public catalog of dictionaries at
    https://migaku-public-data.migaku.com/dicts/index2.json
and the dictionaries themselves at
    https://migaku-public-data.migaku.com/dicts/<lang>/<target>/<slug>.db.gz
where each `*.db.gz` is a gzipped SQLite database.

CRITICAL: the catalog's `url_db` fields are relative paths that need to be
joined against `/dicts/`, not against `/`. The full URL for the Mandarin
default is:
    https://migaku-public-data.migaku.com/dicts/zh_CN/en/migaku-mandarin-dict.json.db.gz

Catalog shape (verified 2026-05-07):

    {
      "languages": [
        {
          "code": "zh_CN",
          "name_en": "Chinese Simplified",
          "to_languages": [
            {
              "code": "en",
              "dictionaries": [
                {
                  "name":     "Migaku Mandarin Dictionary",
                  "default":  true,
                  "url":      "/zh_CN/en/migaku-mandarin-dict.json.zip",
                  "url_db":   "/zh_CN/en/migaku-mandarin-dict.json.db.gz",
                  ...
                },
                ...
              ]
            }
          ],
          "frequency_url_db": "/zh_CN/frequency.db.gz",
          "frequency_lists":  [ ... ]    # alternative ranked sources
        },
        ...
      ]
    }

Dict SQLite schema (Migaku Mandarin Dictionary, verified 2026-05-07):

    CREATE TABLE langResourceEntry(
      id                    INTEGER PRIMARY KEY AUTOINCREMENT,
      langResourceInfoId    INTEGER NOT NULL,
      lang                  TEXT NOT NULL COLLATE NOCASE,    -- 'zh'
      term                  TEXT NOT NULL COLLATE NOCASE,    -- the Hanzi (e.g. '学习')
      backwardTerm          TEXT NOT NULL COLLATE NOCASE,
      displayTerm           TEXT NOT NULL COLLATE NOCASE,
      termAlt               TEXT NOT NULL COLLATE NOCASE,    -- traditional form
      reading               TEXT NOT NULL COLLATE NOCASE,    -- tone-marked pinyin (e.g. 'xué xí')
      backwardReading       TEXT NOT NULL COLLATE NOCASE,
      definition            TEXT NOT NULL                    -- '1. to learn<br>2. to study'
    );
    CREATE INDEX idx_langResourceEntry_term    ON langResourceEntry(term);
    CREATE INDEX idx_langResourceEntry_reading ON langResourceEntry(reading);

    -- The dict file ALSO has empty langFrequencyEntry and langVocabularyEntry
    -- tables (placeholders shared with frequency.db / wordlist DBs).

Polysemous terms get multiple rows (e.g. 行 has two entries: háng and
xíng with different `id`s). The dict's own ordering (ASC by `id`)
appears to match Migaku's `secondary` 0/1/... index — i.e. picking the
N-th row for `secondary=N` returns the same reading the Migaku app would
show for that sense. Confirmed empirically against the 行 case
(secondary=0 -> first entry; secondary=1 -> second entry). When that
heuristic gets disproved by a counter-example, replace with whatever
mapping turns out to be authoritative.

The dict does NOT carry an inline 5-star frequency. Use the separate
`frequency.db` (per-language, top-100k word ranks) for that — see
`migaku.frequency.MigakuFrequency`.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from .. import config


log = logging.getLogger("migaku-notion")


CATALOG_URL = "https://migaku-public-data.migaku.com/dicts/index2.json"
PUBLIC_DATA_BASE = "https://migaku-public-data.migaku.com/dicts"
CATALOG_TTL_SECONDS = 24 * 60 * 60   # 1 day


@dataclass
class DictEntry:
    """One result from `MigakuDict.lookup()`."""

    dict_form: str
    reading: str
    meaning: str
    examples: list[str] = field(default_factory=list)
    parts_of_speech: list[str] = field(default_factory=list)
    sense_index: int = 0
    frequency_stars: int | None = None  # always None for the Mandarin dict
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def _catalog_paths() -> tuple[Path, Path]:
    return (
        config.DICTS_DIR / "index2.json",
        config.DICTS_DIR / "index2.fetched_at",
    )


def get_dict_catalog(*, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch (or load from cache) the public dictionary catalog."""
    catalog_path, fetched_at_path = _catalog_paths()
    if not force_refresh and catalog_path.exists() and fetched_at_path.exists():
        try:
            fetched_at = float(fetched_at_path.read_text().strip())
            if time.time() - fetched_at < CATALOG_TTL_SECONDS:
                return json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    config.DICTS_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Fetching Migaku dict catalog from %s ...", CATALOG_URL)
    resp = requests.get(CATALOG_URL, timeout=30)
    resp.raise_for_status()
    catalog = resp.json()

    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    fetched_at_path.write_text(str(time.time()))
    return catalog


def _normalise_lang_code(lang: str) -> str:
    """Map a Migaku per-word `language` (e.g. 'zh') to a catalog code (e.g. 'zh_CN').

    The catalog uses richer codes (`zh_CN`, `zh_TW`, `pt_BR`, ...) than
    Migaku's per-word `language` field. For the cases we know about,
    pick the simplified Mandarin variant by default.
    """
    aliases = {
        "zh":    "zh_CN",
        "zh-cn": "zh_CN",
        "zh-tw": "zh_TW",
        "pt":    "pt_BR",
    }
    return aliases.get(lang.lower(), lang)


def get_default_dict_for_lang(
    lang: str,
    target_lang: str = "en",
    *,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Pick the default dict entry for `lang` from the catalog.

    Walks `languages[].to_languages[].dictionaries[]` and returns the
    one with `default: true`, or the first available match if none is
    flagged. Caller cares about `name`, `url`, `url_db`, `default`.
    """
    catalog = catalog if catalog is not None else get_dict_catalog()
    langs = catalog.get("languages") or []
    norm = _normalise_lang_code(lang)

    candidates: list[dict[str, Any]] = []
    for lang_entry in langs:
        if not isinstance(lang_entry, dict):
            continue
        code = lang_entry.get("code") or ""
        if code == lang or code == norm or code.startswith(f"{lang}_"):
            candidates.append(lang_entry)

    for lang_entry in candidates:
        for tl in lang_entry.get("to_languages") or []:
            if not isinstance(tl, dict):
                continue
            if target_lang and tl.get("code") != target_lang:
                continue
            dicts = tl.get("dictionaries") or []
            default = next((d for d in dicts if d.get("default")), None)
            if default:
                return default
            if dicts:
                return dicts[0]

    return None


def _absolute_url(url_or_path: str) -> str:
    """Resolve a catalog `url` / `url_db` value to an absolute URL.

    Catalog paths are relative to `/dicts/`, so a `/zh_CN/.../foo.db.gz`
    becomes `https://migaku-public-data.migaku.com/dicts/zh_CN/.../foo.db.gz`.
    """
    if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
        return url_or_path
    if url_or_path.startswith("/"):
        return PUBLIC_DATA_BASE + url_or_path
    return f"{PUBLIC_DATA_BASE}/{url_or_path}"


# ---------------------------------------------------------------------------
# MigakuDict — per-(lang, target_lang) wrapper
# ---------------------------------------------------------------------------

# Definition payloads are HTML-ish: numbered "<br>"-separated senses,
# sometimes with example sentences appended. Reasonably small surface to
# parse with two regexes.
_DEFINITION_LINE_RE = re.compile(r"\s*\d+\.\s*", re.UNICODE)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


class MigakuDict:
    """Wrapper around one of Migaku's published dictionary DBs.

    Construct once per (lang, target_lang) and reuse — the SQLite
    connection opens lazily on the first lookup and stays open until
    `close()` is called.
    """

    def __init__(
        self,
        lang: str,
        target_lang: str = "en",
        cache_dir: Path | None = None,
    ) -> None:
        self.lang = lang
        self.lang_code = _normalise_lang_code(lang)
        self.target_lang = target_lang
        self.cache_dir = cache_dir or config.DICTS_DIR
        self._catalog_entry: dict[str, Any] | None = None
        self._db_path: Path | None = None
        self._conn: sqlite3.Connection | None = None

    # ----- lazy bootstrap ---------------------------------------------

    def _resolve_catalog_entry(self) -> dict[str, Any]:
        if self._catalog_entry is not None:
            return self._catalog_entry
        entry = get_default_dict_for_lang(self.lang, self.target_lang)
        if entry is None:
            raise RuntimeError(
                f"No Migaku dictionary found in the public catalog for "
                f"lang={self.lang!r} target={self.target_lang!r}. "
                f"Catalog: {CATALOG_URL}"
            )
        self._catalog_entry = entry
        return entry

    def ensure_downloaded(self) -> Path:
        """Download + gunzip the dict if we don't already have it locally.

        Returns the absolute path to the un-gzipped SQLite file.
        Idempotent — if the file already exists, returns immediately.
        """
        if self._db_path and self._db_path.exists():
            return self._db_path

        entry = self._resolve_catalog_entry()
        url_db = entry.get("url_db")
        if not url_db:
            raise RuntimeError(
                f"Catalog entry has no `url_db` field: {entry.get('name')!r}"
            )
        url = _absolute_url(url_db)

        # Derive a stable filename from the URL.
        slug = url_db.rsplit("/", 1)[-1]
        if slug.endswith(".gz"):
            db_name = slug[:-3]    # strip .gz
        else:
            db_name = slug
        lang_dir = self.cache_dir / self.lang_code
        lang_dir.mkdir(parents=True, exist_ok=True)
        gz_path = lang_dir / slug
        db_path = lang_dir / db_name

        if not db_path.exists():
            if not gz_path.exists():
                log.info("Downloading %s -> %s ...", url, gz_path.name)
                with requests.get(url, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    with gz_path.open("wb") as fh:
                        for chunk in r.iter_content(chunk_size=1 << 16):
                            if chunk:
                                fh.write(chunk)
            log.info("Decompressing %s -> %s ...", gz_path.name, db_path.name)
            with gzip.open(gz_path, "rb") as src, db_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)

        self._db_path = db_path
        return db_path

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            db = self.ensure_downloaded()
            self._conn = sqlite3.connect(str(db))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    # ----- lookups ----------------------------------------------------

    def lookup(
        self,
        dict_form: str,
        sense_index: str | int = 0,
    ) -> DictEntry | None:
        """Look up a single word + sense in the dictionary.

        Returns None if `dict_form` isn't in the dict at all.

        For polysemous terms (multiple rows per `term`), `sense_index`
        picks the Nth entry in `id`-ascending order (mirrors Migaku's
        own `secondary` 0/1/... convention). If `sense_index` is out
        of range, falls back to the first entry — better than returning
        nothing and matches what the Migaku UI does in the same case.
        """
        if not dict_form:
            return None
        try:
            sense = int(str(sense_index)) if sense_index not in ("", None) else 0
        except (TypeError, ValueError):
            sense = 0
        if sense < 0:
            sense = 0

        conn = self._ensure_conn()
        # Pull every entry for this term in id-ascending order, then
        # pick the Nth. This is cheap because the rows-per-term count
        # is tiny (1..10) and `term` is indexed.
        rows = list(conn.execute(
            "SELECT id, term, displayTerm, termAlt, reading, definition "
            "FROM langResourceEntry WHERE term = ? ORDER BY id ASC LIMIT 32",
            (dict_form,),
        ))
        if not rows:
            return None
        row = rows[sense] if sense < len(rows) else rows[0]
        d = dict(row)

        senses, examples = _split_definition(d["definition"])
        meaning = "; ".join(senses) if senses else (d["definition"] or "")
        return DictEntry(
            dict_form=d["term"],
            reading=d["reading"] or "",
            meaning=meaning,
            examples=examples,
            parts_of_speech=[],   # not in this dict's schema
            sense_index=sense,
            frequency_stars=None, # use MigakuFrequency for stars
            raw=d,
        )

    def frequency_stars(self, dict_form: str) -> int | None:
        """The Migaku Mandarin Dictionary doesn't carry inline frequency stars.
        Always returns None; callers should fall back to `MigakuFrequency`.
        Future dicts (other languages) may populate this; if so, a
        per-dict subclass can override.
        """
        return None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _split_definition(raw: str) -> tuple[list[str], list[str]]:
    """Split a `langResourceEntry.definition` blob into (senses, examples).

    Definitions look like:  '1. to learn<br>2. to study'
    Sometimes with example sentences appended after the senses; the
    Mandarin dict format we observed (CEDICT-derived) keeps everything
    as one numbered list so `examples` ends up empty for now. The
    return type leaves room for richer parsing once we encounter dicts
    that DO carry examples (other Migaku languages, possibly).
    """
    if not raw:
        return [], []
    # Replace <br> with newlines, then split on the leading "N." markers.
    text = _BR_RE.sub("\n", raw)
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    senses: list[str] = []
    examples: list[str] = []
    for p in parts:
        # Strip the leading "1. " / "12. " prefix if present; keep the
        # body. Anything that *isn't* a numbered sense we treat as an
        # example sentence, which gives us forward-compat with dicts
        # that mix the two.
        m = _DEFINITION_LINE_RE.match(p)
        if m:
            senses.append(p[m.end():].strip())
        elif senses:
            # Non-numbered text after at least one sense — likely an example.
            examples.append(p)
        else:
            senses.append(p)
    return senses, examples

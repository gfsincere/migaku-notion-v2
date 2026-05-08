"""Client for Migaku's published per-language frequency database.

URL convention (per the catalog's `frequency_url_db` field):
    https://migaku-public-data.migaku.com/dicts/<lang>/frequency.db.gz

For zh_CN: 100,000 rows ranking the most common terms 1..100,000.

Schema (verified 2026-05-07):

    CREATE TABLE langFrequencyEntry(
      id                  INTEGER PRIMARY KEY AUTOINCREMENT,
      langResourceInfoId  INTEGER NOT NULL,
      lang                TEXT NOT NULL COLLATE NOCASE,
      term                TEXT NOT NULL COLLATE NOCASE,
      backwardTerm        TEXT NOT NULL COLLATE NOCASE,
      displayTerm         TEXT NOT NULL COLLATE NOCASE,
      termAlt             TEXT NOT NULL COLLATE NOCASE,
      reading             TEXT NOT NULL COLLATE NOCASE,
      backwardReading     TEXT NOT NULL COLLATE NOCASE,
      frequency           INTEGER NOT NULL                  -- the rank, 1=most common
    );
    CREATE INDEX idx_langFrequencyEntry_term ON langFrequencyEntry(term);

Bucketing
---------

Migaku's UI shows a 1-5 star rating where 5 = most common. With 100k
rows we use plain quintile bucketing:

    rank in [    1,  20000]  -> 5
    rank in [20001,  40000]  -> 4
    rank in [40001,  60000]  -> 3
    rank in [60001,  80000]  -> 2
    rank in [80001, 100000]  -> 1
    not in DB                -> None

The total row count is read from the DB at first lookup and cached, so
the bucketing also Just Works for non-zh languages whose frequency
list happens to be a different size.

If a future schema change shows that Migaku itself uses non-quintile
boundaries (logarithmic, top-1k=5 hard cutoff, etc.), spot-check 20
words against the extension UI and update `_stars_for_rank` here.
Document the deviation in DESIGN-PRINCIPLES.md.
"""
from __future__ import annotations

import gzip
import logging
import shutil
import sqlite3
from pathlib import Path

import requests

from .. import config
from . import dict as _dict_mod  # for catalog access; named to avoid clash


log = logging.getLogger("migaku-notion")


class MigakuFrequency:
    """Read-only wrapper around `<lang>/frequency.db`."""

    def __init__(self, lang: str, cache_dir: Path | None = None) -> None:
        self.lang = lang
        self.lang_code = _dict_mod._normalise_lang_code(lang)
        self.cache_dir = cache_dir or config.DICTS_DIR
        self._db_path: Path | None = None
        self._conn: sqlite3.Connection | None = None
        self._total_rows: int | None = None

    def _resolve_url(self) -> str:
        """Pull the per-language frequency-db URL from the public catalog."""
        catalog = _dict_mod.get_dict_catalog()
        languages = catalog.get("languages") or []
        entry = None
        for lang_entry in languages:
            if not isinstance(lang_entry, dict):
                continue
            code = lang_entry.get("code") or ""
            if code == self.lang or code == self.lang_code or code.startswith(f"{self.lang}_"):
                entry = lang_entry
                break
        url_db = (entry or {}).get("frequency_url_db")
        if not url_db:
            raise RuntimeError(
                f"No frequency_url_db in catalog for lang={self.lang!r}. "
                f"Catalog: {_dict_mod.CATALOG_URL}"
            )
        return _dict_mod._absolute_url(url_db)

    def ensure_downloaded(self) -> Path:
        if self._db_path and self._db_path.exists():
            return self._db_path

        url = self._resolve_url()
        slug = url.rsplit("/", 1)[-1]      # 'frequency.db.gz'
        db_name = slug[:-3] if slug.endswith(".gz") else slug
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

    def _ensure_total(self) -> int:
        if self._total_rows is None:
            row = self._ensure_conn().execute(
                "SELECT COUNT(*) FROM langFrequencyEntry"
            ).fetchone()
            self._total_rows = int(row[0]) if row else 0
        return self._total_rows

    # ----- public API -------------------------------------------------

    def rank(self, dict_form: str) -> int | None:
        """Raw rank: 1 = most common, increasing. None if not in the list.

        For polysemous terms there's at most one row per `term` in
        the frequency list (Migaku doesn't disambiguate by reading
        for the rank table — confirmed by the schema), so this is a
        single SELECT.
        """
        if not dict_form:
            return None
        row = self._ensure_conn().execute(
            "SELECT frequency FROM langFrequencyEntry WHERE term = ? "
            "ORDER BY frequency ASC LIMIT 1",
            (dict_form,),
        ).fetchone()
        if row is None:
            return None
        return int(row[0])

    def stars(self, dict_form: str) -> int | None:
        """Bucket the raw rank into a Migaku-style 1-5 quintile."""
        r = self.rank(dict_form)
        if r is None:
            return None
        total = self._ensure_total()
        if total <= 0:
            return None
        return _stars_for_rank(r, total)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _stars_for_rank(rank: int, total: int) -> int:
    """Map (rank, total) -> 1..5, with 5 = most common (top quintile).

    Quintile boundaries are inclusive on the upper edge:
        rank <=     total/5  -> 5
        rank <=  2 *total/5  -> 4
        rank <=  3 *total/5  -> 3
        rank <=  4 *total/5  -> 2
        else                  -> 1
    """
    for stars, mult in ((5, 1), (4, 2), (3, 3), (2, 4)):
        if rank * 5 <= mult * total:
            return stars
    return 1

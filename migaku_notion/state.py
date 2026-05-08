"""Persistent local diff cache backing `state.db`.

Ported directly from v1's `sync.py`; the `words` table schema is
byte-for-byte identical so users can copy a v1 `state.db` into v2's
project root and it just works. The migration shims (`pinyin` ->
`pinyin_marks`, ALTER TABLE ADD COLUMN for newer fields) are preserved.

v2 additions (forward-compatible — v1 tools will just ignore them):
  - `meta` table: a single-row key-value store for sync metadata.
    Tracks `last_server_version` (so daily syncs can pull incrementally
    from /pull-sync), `device_id` (mirror of MIGAKU_DEVICE_ID for cross-
    validation), `last_full_pull_at` (ISO timestamp of the most recent
    serverVersion=0 sweep), and `v2_first_sync_done` ("1"/"0"; gates
    the one-time auto-populate of blank Meanings).
  - `meaning`, `example`, `frequency_stars`, `notion_meaning_was_blank`
    columns on `words` (added via idempotent ALTER TABLE; v1 tools will
    keep working since they just SELECT * over the columns they know
    about).

Atomicity rule (carried from v1): every Notion API call is paired with a
single `cache.upsert(row)` inside its own transaction. A SIGKILL between
the two would lose at most one row's worth of state, recoverable by
`rebuild-cache`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .models import CachedRow


class StateCache:
    """SQLite-backed diff cache. One row per Migaku key seen so far."""

    SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS words (
        migaku_key      TEXT PRIMARY KEY,
        page_id         TEXT NOT NULL,
        lang            TEXT,
        dict_form       TEXT,
        secondary       TEXT,
        known_status    TEXT,
        fail_rate       REAL,
        total_reviews   INTEGER,
        failed_reviews  INTEGER,
        part_of_speech  TEXT,
        last_synced     TEXT,
        archived        INTEGER NOT NULL DEFAULT 0,
        pinyin_marks    TEXT,
        pinyin_numeric  TEXT,
        sense_index     TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_words_lang ON words(lang);

    -- v2 only. Single-row key-value store for sync metadata. v1 doesn't
    -- read this table, so a v1 binary opening a v2-touched state.db is
    -- still fine.
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """

    # The keys we read/write in the meta table. Centralised so typos at
    # call sites don't silently lose data.
    META_LAST_SERVER_VERSION = "last_server_version"
    META_DEVICE_ID           = "device_id"
    META_LAST_FULL_PULL_AT   = "last_full_pull_at"
    META_V2_FIRST_SYNC_DONE  = "v2_first_sync_done"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        with self.conn:
            self.conn.executescript(self.SCHEMA_SQL)

        # Idempotent migrations. v1 had a `pinyin` -> `pinyin_marks`
        # rename and three later ADD COLUMN steps; v2 piggybacks four
        # more ADDs on top. All are wrapped in their own transaction so
        # an interrupted run still makes forward progress next time.
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(words)")}
        if "pinyin" in cols and "pinyin_marks" not in cols:
            with self.conn:
                self.conn.execute("ALTER TABLE words RENAME COLUMN pinyin TO pinyin_marks")
            cols.add("pinyin_marks")
            cols.discard("pinyin")
        text_cols = (
            "pinyin_marks", "pinyin_numeric", "sense_index",
            "meaning", "example",                         # v2
        )
        for col in text_cols:
            if col not in cols:
                with self.conn:
                    self.conn.execute(f"ALTER TABLE words ADD COLUMN {col} TEXT")
        if "frequency_stars" not in cols:
            with self.conn:
                self.conn.execute("ALTER TABLE words ADD COLUMN frequency_stars INTEGER")
        if "notion_meaning_was_blank" not in cols:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE words ADD COLUMN notion_meaning_was_blank "
                    "INTEGER NOT NULL DEFAULT 1"
                )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "StateCache":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def load_all(self) -> dict[str, CachedRow]:
        out: dict[str, CachedRow] = {}
        for r in self.conn.execute("SELECT * FROM words"):
            keys = r.keys()
            out[r["migaku_key"]] = CachedRow(
                migaku_key=r["migaku_key"],
                page_id=r["page_id"],
                lang=r["lang"],
                dict_form=r["dict_form"],
                secondary=r["secondary"],
                known_status=r["known_status"],
                fail_rate=r["fail_rate"],
                total_reviews=r["total_reviews"],
                failed_reviews=r["failed_reviews"],
                part_of_speech=r["part_of_speech"],
                last_synced=r["last_synced"],
                archived=bool(r["archived"]),
                pinyin_marks=r["pinyin_marks"],
                pinyin_numeric=r["pinyin_numeric"],
                sense_index=r["sense_index"],
                # v2 columns. Use `in keys` so that a v1 state.db that's
                # been opened by v2 (and migrated up) is still readable
                # from a v2 binary that bypasses the migration somehow.
                meaning=r["meaning"] if "meaning" in keys else None,
                example=r["example"] if "example" in keys else None,
                frequency_stars=(r["frequency_stars"] if "frequency_stars" in keys else None),
                notion_meaning_was_blank=(
                    bool(r["notion_meaning_was_blank"])
                    if "notion_meaning_was_blank" in keys else True
                ),
            )
        return out

    def upsert(self, row: CachedRow) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO words (migaku_key, page_id, lang, dict_form, secondary,
                                   known_status, fail_rate, total_reviews, failed_reviews,
                                   part_of_speech, last_synced, archived,
                                   pinyin_marks, pinyin_numeric, sense_index,
                                   meaning, example, frequency_stars,
                                   notion_meaning_was_blank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(migaku_key) DO UPDATE SET
                    page_id                  = excluded.page_id,
                    lang                     = excluded.lang,
                    dict_form                = excluded.dict_form,
                    secondary                = excluded.secondary,
                    known_status             = excluded.known_status,
                    fail_rate                = excluded.fail_rate,
                    total_reviews            = excluded.total_reviews,
                    failed_reviews           = excluded.failed_reviews,
                    part_of_speech           = excluded.part_of_speech,
                    last_synced              = excluded.last_synced,
                    archived                 = excluded.archived,
                    pinyin_marks             = excluded.pinyin_marks,
                    pinyin_numeric           = excluded.pinyin_numeric,
                    sense_index              = excluded.sense_index,
                    meaning                  = excluded.meaning,
                    example                  = excluded.example,
                    frequency_stars          = excluded.frequency_stars,
                    notion_meaning_was_blank = excluded.notion_meaning_was_blank
                """,
                (row.migaku_key, row.page_id, row.lang, row.dict_form, row.secondary,
                 row.known_status, row.fail_rate, row.total_reviews, row.failed_reviews,
                 row.part_of_speech, row.last_synced, 1 if row.archived else 0,
                 row.pinyin_marks, row.pinyin_numeric, row.sense_index,
                 row.meaning, row.example, row.frequency_stars,
                 1 if row.notion_meaning_was_blank else 0),
            )

    def mark_archived(self, key: str, archived: bool, last_synced: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE words SET archived = ?, last_synced = COALESCE(?, last_synced) "
                "WHERE migaku_key = ?",
                (1 if archived else 0, last_synced, key),
            )

    def stats(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN archived=1 THEN 1 ELSE 0 END) AS archived, "
            "MAX(last_synced) AS last_synced "
            "FROM words"
        ).fetchone()
        return {
            "total": row["total"] or 0,
            "archived": row["archived"] or 0,
            "last_synced": row["last_synced"],
        }

    # -----------------------------------------------------------------
    # meta key-value store (sync metadata: server version, device id, ...)
    # -----------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        return row["value"]

    def set_meta(self, key: str, value: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_server_version(self) -> int:
        """Resume token for /pull-sync. 0 if never pulled (= full refresh)."""
        raw = self.get_meta(self.META_LAST_SERVER_VERSION)
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    def set_server_version(self, server_version: int) -> None:
        self.set_meta(self.META_LAST_SERVER_VERSION, str(int(server_version)))

    def get_device_id(self) -> str | None:
        """Cache-side mirror of MIGAKU_DEVICE_ID (for cross-validation)."""
        return self.get_meta(self.META_DEVICE_ID)

    def set_device_id(self, device_id: str) -> None:
        self.set_meta(self.META_DEVICE_ID, device_id)

    def get_last_full_pull_at(self) -> str | None:
        return self.get_meta(self.META_LAST_FULL_PULL_AT)

    def mark_full_pull(self, when_iso: str) -> None:
        self.set_meta(self.META_LAST_FULL_PULL_AT, when_iso)

    def is_v2_first_sync_done(self) -> bool:
        """True if a v2 sync has completed against this cache.

        Gates the one-time auto-population of blank Meanings (Greg,
        2026-05-07): on the first v2 sync, dict-derived Meanings get
        written into Notion rows whose Meaning is currently blank. From
        the second sync onward, Meaning is left alone (matches v1's
        "never overwrite Meaning" rule).
        """
        return self.get_meta(self.META_V2_FIRST_SYNC_DONE) == "1"

    def mark_v2_first_sync_done(self) -> None:
        self.set_meta(self.META_V2_FIRST_SYNC_DONE, "1")


# ---------------------------------------------------------------------------
# Diff helpers (used by sync to skip unchanged rows)
# ---------------------------------------------------------------------------

def _approx_eq(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) < 1e-6


def has_tracked_changes(word: Any, cached: CachedRow) -> bool:
    """Return True if any tracked field on `word` differs from `cached`.

    Tracked fields (must match the columns Notion's `update_page` writes,
    so the sync can skip a Notion call when nothing of consequence has
    changed):

      v1 set:
        known_status, fail_rate, total_reviews, failed_reviews,
        part_of_speech, pinyin_marks, pinyin_numeric, sense_index
      v2 additions:
        meaning, example, frequency_stars

    `Meaning` is special — see `is_v2_first_sync_done` and the sync
    flow notes. We DO compare it here, but the sync flow is responsible
    for only INCLUDING it in the update payload when the first-sync
    auto-populate is allowed. After that, has_tracked_changes still
    flagging a Meaning diff is harmless: if Meaning is the only thing
    that changed and we're not allowed to write it, the resulting
    update payload will be a no-op (and the sync should detect that
    and skip the call altogether).
    """
    if (word.known_status or None) != cached.known_status:
        return True
    word_fail = round(word.fail_rate, 2) if word.fail_rate is not None else None
    if not _approx_eq(word_fail, cached.fail_rate):
        return True
    if word.total_reviews != cached.total_reviews:
        return True
    if word.failed_reviews != cached.failed_reviews:
        return True
    if (word.part_of_speech or None) != cached.part_of_speech:
        return True
    if (word.pinyin_marks or None) != cached.pinyin_marks:
        return True
    if (word.pinyin_numeric or None) != cached.pinyin_numeric:
        return True
    word_sense = word.secondary if word.language == "zh" else None
    if (word_sense or None) != cached.sense_index:
        return True
    if (getattr(word, "meaning", None) or None) != cached.meaning:
        return True
    if (getattr(word, "example", None) or None) != cached.example:
        return True
    if getattr(word, "frequency_stars", None) != cached.frequency_stars:
        return True
    return False

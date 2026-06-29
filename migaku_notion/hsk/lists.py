"""Download and cache HSK 2.0 / 3.0 vocabulary lists.

Source (MIT): https://github.com/drkameleon/complete-hsk-vocabulary
  - HSK 2.0 inclusive: wordlists/inclusive/old/{1-6}.min.json
  - HSK 3.0 inclusive: wordlists/inclusive/newest/{1-7}.min.json
  - Exclusive lists live under wordlists/exclusive/{old|newest}/

We cache a compact JSON file with simplified-character word lists only.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .. import config


log = logging.getLogger("migaku-notion")

HSK_SOURCE_REPO = "drkameleon/complete-hsk-vocabulary"
HSK_RAW_BASE = (
    "https://raw.githubusercontent.com/drkameleon/complete-hsk-vocabulary/main"
)
CACHE_FILENAME = "lists-compact.json"

HSK20_LEVELS = range(1, 7)   # 1..6
HSK30_LEVELS = range(1, 8)   # 1..7 (7 = new syllabus bands 7-9)


@dataclass(frozen=True)
class HskLists:
    """In-memory HSK word sets keyed by level string ('1'..'6' or '1'..'7')."""

    hsk20_inclusive: dict[str, frozenset[str]]
    hsk20_exclusive: dict[str, frozenset[str]]
    hsk30_inclusive: dict[str, frozenset[str]]
    hsk30_exclusive: dict[str, frozenset[str]]
    fetched_at: str
    source: str = HSK_SOURCE_REPO


def _words_from_min_payload(data: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        word = entry.get("s") or entry.get("simplified")
        if isinstance(word, str) and word.strip():
            out.append(word.strip())
    return out


def _fetch_level_words(path: str) -> list[str]:
    url = f"{HSK_RAW_BASE}/{path}"
    log.info("Fetching HSK list %s", path)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected HSK list shape at {path}")
    return _words_from_min_payload(data)


def _level_map(
    *,
    standard: str,
    mode: str,
    levels: range,
) -> dict[str, frozenset[str]]:
    folder = {
        ("hsk20", "inclusive"): ("wordlists/inclusive/old", ".min.json"),
        ("hsk20", "exclusive"): ("wordlists/exclusive/old", ".min.json"),
        ("hsk30", "inclusive"): ("wordlists/inclusive/newest", ".min.json"),
        ("hsk30", "exclusive"): ("wordlists/exclusive/newest", ".min.json"),
    }[(standard, mode)]
    prefix, suffix = folder
    out: dict[str, frozenset[str]] = {}
    for level in levels:
        path = f"{prefix}/{level}{suffix}"
        words = _fetch_level_words(path)
        out[str(level)] = frozenset(words)
    return out


def _build_compact_cache() -> dict[str, Any]:
    log.info("Building HSK list cache from %s ...", HSK_SOURCE_REPO)
    return {
        "source": HSK_SOURCE_REPO,
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "hsk20": {
            "inclusive": {
                str(k): sorted(v)
                for k, v in _level_map(
                    standard="hsk20", mode="inclusive", levels=HSK20_LEVELS
                ).items()
            },
            "exclusive": {
                str(k): sorted(v)
                for k, v in _level_map(
                    standard="hsk20", mode="exclusive", levels=HSK20_LEVELS
                ).items()
            },
        },
        "hsk30": {
            "inclusive": {
                str(k): sorted(v)
                for k, v in _level_map(
                    standard="hsk30", mode="inclusive", levels=HSK30_LEVELS
                ).items()
            },
            "exclusive": {
                str(k): sorted(v)
                for k, v in _level_map(
                    standard="hsk30", mode="exclusive", levels=HSK30_LEVELS
                ).items()
            },
        },
    }


def _cache_path(cache_dir: Path | None = None) -> Path:
    root = cache_dir or config.HSK_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root / CACHE_FILENAME


def _to_frozenset_map(raw: dict[str, list[str]]) -> dict[str, frozenset[str]]:
    return {k: frozenset(v) for k, v in raw.items()}


def ensure_hsk_lists(*, cache_dir: Path | None = None, refresh: bool = False) -> HskLists:
    """Return cached HSK lists, downloading on first use unless *refresh*."""
    path = _cache_path(cache_dir)
    if refresh or not path.is_file():
        compact = _build_compact_cache()
        path.write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Wrote HSK cache to %s", path)
    else:
        compact = json.loads(path.read_text(encoding="utf-8"))

    h20 = compact.get("hsk20") or {}
    h30 = compact.get("hsk30") or {}
    return HskLists(
        hsk20_inclusive=_to_frozenset_map(h20.get("inclusive") or {}),
        hsk20_exclusive=_to_frozenset_map(h20.get("exclusive") or {}),
        hsk30_inclusive=_to_frozenset_map(h30.get("inclusive") or {}),
        hsk30_exclusive=_to_frozenset_map(h30.get("exclusive") or {}),
        fetched_at=str(compact.get("fetched_at") or ""),
        source=str(compact.get("source") or HSK_SOURCE_REPO),
    )

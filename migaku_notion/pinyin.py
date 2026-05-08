"""Tone-marked and numeric pinyin generation.

Ported verbatim from v1's `sync.py`; pypinyin is optional at import time
(so non-zh syncs still work without it) but required at run time when the
caller actually requests Mandarin pinyin.
"""
from __future__ import annotations


try:
    from pypinyin import lazy_pinyin, Style as _PinyinStyle  # type: ignore
    PINYIN_AVAILABLE = True
except ImportError:
    PINYIN_AVAILABLE = False
    _PinyinStyle = None  # type: ignore


def compute_pinyin_marks(hanzi: str) -> str:
    """Generate tone-marked pinyin (e.g. 'xué xí'). Empty string if pypinyin unavailable."""
    if not hanzi or not PINYIN_AVAILABLE:
        return ""
    return " ".join(lazy_pinyin(hanzi, style=_PinyinStyle.TONE))


def compute_pinyin_numeric(hanzi: str) -> str:
    """Generate numeric-tone pinyin (e.g. 'xue2 xi2'). Empty string if pypinyin unavailable."""
    if not hanzi or not PINYIN_AVAILABLE:
        return ""
    return " ".join(lazy_pinyin(hanzi, style=_PinyinStyle.TONE3))

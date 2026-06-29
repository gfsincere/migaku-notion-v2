"""CJK ideograph helpers shared by `chars` and Notion sync totals."""
from __future__ import annotations


def is_cjk(ch: str) -> bool:
    """True if `ch` is a CJK ideograph (Han character)."""
    if not ch:
        return False
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x20000 <= cp <= 0x2A6DF
    )


def add_cjk_chars(text: str, into: set[str]) -> None:
    """Add every CJK character in `text` to `into`."""
    for ch in text:
        if is_cjk(ch):
            into.add(ch)


def known_word_and_char_totals(dict_forms: list[str]) -> tuple[int, int]:
    """Return (known_word_count, unique_known_cjk_char_count)."""
    chars: set[str] = set()
    for text in dict_forms:
        add_cjk_chars(text, chars)
    return len(dict_forms), len(chars)

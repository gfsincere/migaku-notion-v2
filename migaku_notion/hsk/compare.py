"""Compare KNOWN vocabulary against HSK 2.0 / 3.0 syllabi."""
from __future__ import annotations

from typing import Any

from ..state import StateCache
from .lists import HSK20_LEVELS, HSK30_LEVELS, HskLists, ensure_hsk_lists

# Fraction of a level's syllabus you must know to count as "at" that level.
DEFAULT_LEVEL_THRESHOLD = 0.80


def known_word_forms(cache: StateCache, lang: str) -> set[str]:
    """All non-archived KNOWN `dict_form` values for a language."""
    return words_by_status(cache, lang).get("KNOWN", set())


def words_by_status(cache: StateCache, lang: str) -> dict[str, set[str]]:
    """Group non-archived `dict_form` values by upper-case Migaku status."""
    out: dict[str, set[str]] = {}
    for r in cache.conn.execute(
        "SELECT dict_form, known_status FROM words "
        "WHERE lang = ? AND archived = 0",
        (lang,),
    ):
        form = (r["dict_form"] or "").strip()
        if not form:
            continue
        status = (r["known_status"] or "UNKNOWN").upper()
        out.setdefault(status, set()).add(form)
    return out


def _status_for_word(word: str, by_status: dict[str, set[str]]) -> str | None:
    """Best status for *word* if present in cache (KNOWN beats LEARNING)."""
    if word in by_status.get("KNOWN", set()):
        return "KNOWN"
    if word in by_status.get("LEARNING", set()):
        return "LEARNING"
    for status, forms in by_status.items():
        if word in forms:
            return status
    return None


def _level_word_gaps(
    syllabus: frozenset[str],
    by_status: dict[str, set[str]],
) -> dict[str, Any]:
    known: list[str] = []
    learning: list[str] = []
    missing: list[str] = []
    for word in sorted(syllabus):
        status = _status_for_word(word, by_status)
        if status == "KNOWN":
            known.append(word)
        elif status == "LEARNING":
            learning.append(word)
        else:
            missing.append(word)
    return {
        "total": len(syllabus),
        "known_count": len(known),
        "learning_count": len(learning),
        "missing_count": len(missing),
        "known": known,
        "learning": learning,
        "missing": missing,
    }


def build_hsk_gaps_report(
    by_status: dict[str, set[str]],
    lists: HskLists,
    *,
    standard: str = "hsk30",
    mode: str = "exclusive",
) -> dict[str, Any]:
    """Per-level word lists: known, learning (in Migaku), and missing."""
    standard = standard.lower()
    mode = mode.lower()
    if standard == "hsk20":
        levels = HSK20_LEVELS
        syllabi = lists.hsk20_inclusive if mode == "inclusive" else lists.hsk20_exclusive
        label = "HSK 2.0"
    elif standard == "hsk30":
        levels = HSK30_LEVELS
        syllabi = lists.hsk30_inclusive if mode == "inclusive" else lists.hsk30_exclusive
        label = "HSK 3.0"
    else:
        raise ValueError(f"Unsupported standard: {standard!r} (use hsk20 or hsk30)")

    if mode not in ("inclusive", "exclusive"):
        raise ValueError(f"Unsupported mode: {mode!r} (use inclusive or exclusive)")

    level_rows: list[dict[str, Any]] = []
    for level in levels:
        key = str(level)
        gaps = _level_word_gaps(syllabi.get(key, frozenset()), by_status)
        level_rows.append({"level": level, **gaps})

    return {
        "standard": standard,
        "label": label,
        "mode": mode,
        "levels": level_rows,
    }


def build_hsk_gaps_from_cache(
    cache: StateCache,
    lang: str,
    *,
    standard: str = "hsk30",
    mode: str = "exclusive",
    refresh_lists: bool = False,
) -> dict[str, Any]:
    lists = ensure_hsk_lists(refresh=refresh_lists)
    by_status = words_by_status(cache, lang)
    report = build_hsk_gaps_report(by_status, lists, standard=standard, mode=mode)
    report["lang"] = lang
    report["lists_source"] = lists.source
    return report


def build_hsk_report(
    known: set[str],
    lists: HskLists,
    *,
    threshold: float = DEFAULT_LEVEL_THRESHOLD,
) -> dict[str, Any]:
    """JSON-ready HSK coverage report."""
    return {
        "known_word_count": len(known),
        "lists_fetched_at": lists.fetched_at,
        "lists_source": lists.source,
        "hsk20": _standard_report(
            known,
            label="HSK 2.0",
            levels=HSK20_LEVELS,
            inclusive=lists.hsk20_inclusive,
            exclusive=lists.hsk20_exclusive,
            threshold=threshold,
        ),
        "hsk30": _standard_report(
            known,
            label="HSK 3.0",
            levels=HSK30_LEVELS,
            inclusive=lists.hsk30_inclusive,
            exclusive=lists.hsk30_exclusive,
            threshold=threshold,
        ),
    }


def _level_stats(known: set[str], syllabus: frozenset[str]) -> dict[str, Any]:
    total = len(syllabus)
    if total == 0:
        return {"total": 0, "known": 0, "pct": 0.0}
    hit = len(known & syllabus)
    return {
        "total": total,
        "known": hit,
        "pct": round(100.0 * hit / total, 1),
    }


def _estimate_level(
    inclusive_stats: list[dict[str, Any]],
    *,
    threshold: float = DEFAULT_LEVEL_THRESHOLD,
) -> int | None:
    """Highest level whose inclusive syllabus is >= threshold known."""
    best: int | None = None
    for row in inclusive_stats:
        level = int(row["level"])
        if row["total"] and (row["known"] / row["total"]) >= threshold:
            best = level
    return best


def _standard_report(
    known: set[str],
    *,
    label: str,
    levels: range,
    inclusive: dict[str, frozenset[str]],
    exclusive: dict[str, frozenset[str]],
    threshold: float,
) -> dict[str, Any]:
    inc_rows: list[dict[str, Any]] = []
    exc_rows: list[dict[str, Any]] = []
    for level in levels:
        key = str(level)
        inc = _level_stats(known, inclusive.get(key, frozenset()))
        exc = _level_stats(known, exclusive.get(key, frozenset()))
        inc_rows.append({"level": level, **inc})
        exc_rows.append({"level": level, **exc})

    estimated = _estimate_level(inc_rows, threshold=threshold)
    next_level = (estimated + 1) if estimated is not None else 1
    next_row = next((r for r in inc_rows if r["level"] == next_level), None)

    return {
        "label": label,
        "estimated_level": estimated,
        "threshold_pct": round(threshold * 100),
        "inclusive": inc_rows,
        "exclusive": exc_rows,
        "next_level": (
            {
                "level": next_row["level"],
                "known": next_row["known"],
                "total": next_row["total"],
                "pct": next_row["pct"],
                "remaining": next_row["total"] - next_row["known"],
            }
            if next_row and estimated is not None and next_row["level"] > estimated
            else None
        ),
    }


def build_hsk_report_from_cache(
    cache: StateCache,
    lang: str,
    *,
    refresh_lists: bool = False,
    threshold: float = DEFAULT_LEVEL_THRESHOLD,
) -> dict[str, Any]:
    lists = ensure_hsk_lists(refresh=refresh_lists)
    known = words_by_status(cache, lang).get("KNOWN", set())
    report = build_hsk_report(known, lists, threshold=threshold)
    report["lang"] = lang
    return report

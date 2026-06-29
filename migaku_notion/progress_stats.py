"""Shared progress math for CLI output and the local dashboard API."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .state import ProgressSnapshot


def parse_snapshot_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def delta_since(
    snapshots: list[ProgressSnapshot],
    *,
    days: int,
) -> tuple[int, int, int] | None:
    """Return (days_elapsed, d_words, d_chars) vs the nearest snapshot on/before."""
    if len(snapshots) < 2:
        return None
    latest = snapshots[-1]
    target = parse_snapshot_date(latest.snapshot_date)
    anchor_date = target.fromordinal(target.toordinal() - days)
    anchor: ProgressSnapshot | None = None
    for snap in snapshots:
        if parse_snapshot_date(snap.snapshot_date) <= anchor_date:
            anchor = snap
    if anchor is None:
        anchor = snapshots[0]
    elapsed = (
        parse_snapshot_date(latest.snapshot_date)
        - parse_snapshot_date(anchor.snapshot_date)
    ).days
    if elapsed <= 0:
        return None
    return (
        elapsed,
        latest.known_words - anchor.known_words,
        latest.known_chars - anchor.known_chars,
    )


def pace_per_day(delta: tuple[int, int, int] | None) -> dict[str, float] | None:
    if delta is None:
        return None
    elapsed, d_words, d_chars = delta
    return {
        "days": elapsed,
        "words_per_day": d_words / elapsed,
        "chars_per_day": d_chars / elapsed,
        "words_total": d_words,
        "chars_total": d_chars,
    }


def build_progress_payload(
    snapshots: list[ProgressSnapshot],
    *,
    lang: str,
) -> dict[str, Any]:
    """JSON-serialisable report for the dashboard API."""
    points: list[dict[str, Any]] = []
    prev: ProgressSnapshot | None = None
    for snap in snapshots:
        dw = snap.known_words - prev.known_words if prev else None
        dc = snap.known_chars - prev.known_chars if prev else None
        points.append(
            {
                "date": snap.snapshot_date,
                "known_words": snap.known_words,
                "known_chars": snap.known_chars,
                "delta_words": dw,
                "delta_chars": dc,
            }
        )
        prev = snap

    latest = snapshots[-1] if snapshots else None
    return {
        "lang": lang,
        "snapshot_count": len(snapshots),
        "latest": (
            {
                "date": latest.snapshot_date,
                "known_words": latest.known_words,
                "known_chars": latest.known_chars,
            }
            if latest
            else None
        ),
        "points": points,
        "pace": {
            "7d": pace_per_day(delta_since(snapshots, days=7)),
            "30d": pace_per_day(delta_since(snapshots, days=30)),
        },
    }

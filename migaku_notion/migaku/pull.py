"""Read side: GET core-server.migaku.com/pull-sync.

Replaces v1's `MigokuClient.list_words` and the SRS-DB download in the Go
service. One endpoint pulls every entity Migaku tracks for the account,
delta-style by `serverVersion`.

Endpoint (from HAR):
    GET https://core-server.migaku.com/pull-sync
        ?serverVersion=<int>
        &deviceId=<32-hex>
    Authorization: Bearer <firebase id_token>

Response (Content-Encoding: gzip; Content-Type: application/json):
    {
      "decks":             [...],
      "cardTypes":         [...],
      "cards":             [...],
      "cardWordRelations": [...],
      "vacations":         [...],
      "reviews":           [...],     # individual review events
      "words":             [...],     # MigakuWord shape (see migoku_api.go)
      "config":            {...} | null,
      "keyValue":          [...],
      "learningMaterials": [...],
      "lessons":           [...],
      "reviewHistory":     [...],
      "wordHistory":       [...],     # NEW vs v1's payload
      "libraryItems":      [...]      # NEW vs v1's payload
    }

`requests` decompresses gzip transparently — no manual handling needed.

The browser extension polls /pull-sync repeatedly, incrementing
`serverVersion` until the response is fully empty (every array len 0). v2
exposes both modes:

  * Incremental (the default for `sync`) — pass the last `server_version`
    you persisted in state.db's `meta` table. Migaku returns only what's
    new since then; typical payload is tiny. Fast for daily syncs.

  * Full refresh — pass `server_version=0`. Migaku returns the *whole*
    word list (and every other array) as if you were a fresh device.
    Slow first-sync, but combined with v2's diff cache the Notion side
    only PATCHes rows whose tracked fields actually changed, so the
    follow-on cost is minimal. Use this for periodic divergence checks
    against the local cache (Greg's `--full-refresh` flag).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Union

import requests

from ..models import MigakuEntity, Word
from . import auth, const


log = logging.getLogger("migaku-notion")


# Either a stateful AuthSession or a one-shot AuthToken — pull_sync handles both.
AuthLike = Union[auth.AuthSession, auth.AuthToken]


def _bearer_headers(token: AuthLike) -> dict[str, str]:
    if isinstance(token, auth.AuthSession):
        return token.bearer_headers()
    fresh = auth.ensure_fresh(token)
    return {"Authorization": f"Bearer {fresh.id_token}"}


# Migaku's core-server validates Bearer tokens via Firebase Admin. When
# Google's cert endpoint flakes on *their* side, the backend returns 401
# with messages like "Error while fetching public key certificates:
# Request Interrupted". Re-issuing the same id_token after a short backoff
# usually succeeds — refreshing the token does not help.
_TRANSIENT_AUTH_MARKERS = (
    "public key certificates",
    "FirebaseAuthException",
    "CERTIFICATE_FETCH_FAILED",
)
_PULL_SYNC_AUTH_RETRIES = 5


def _is_transient_migaku_auth_error(status_code: int, body: str) -> bool:
    if status_code != 401:
        return False
    lower = body.lower()
    return any(marker.lower() in lower for marker in _TRANSIENT_AUTH_MARKERS)


def _get_pull_sync(
    token: AuthLike,
    *,
    params: dict[str, Any],
    max_attempts: int = _PULL_SYNC_AUTH_RETRIES,
) -> requests.Response:
    headers = _bearer_headers(token)
    last: requests.Response | None = None
    for attempt in range(max_attempts):
        resp = requests.get(
            const.PULL_SYNC_URL,
            params=params,
            headers=headers,
            timeout=120,
        )
        last = resp
        if resp.ok or not _is_transient_migaku_auth_error(resp.status_code, resp.text):
            return resp
        if attempt + 1 >= max_attempts:
            break
        wait = min(2 ** attempt, 10)
        log.warning(
            "Migaku auth validation flake (attempt %d/%d); retrying in %ds ...",
            attempt + 1,
            max_attempts,
            wait,
        )
        time.sleep(wait)
    assert last is not None
    return last


def pull_sync(
    token: AuthLike,
    device_id: str,
    server_version: int = const.PULL_SYNC_INITIAL_SERVER_VERSION,
    *,
    paginate: bool = True,
    max_pages: int = 200,
    fallback_full_on_500: bool = True,
) -> dict[str, Any]:
    """Fetch the raw `migakuSyncPayload` dict from /pull-sync.

    Pass `server_version=0` for a full refresh (every word + card +
    review the account has ever produced). Pass the value returned by
    `StateCache.get_server_version()` to resume incrementally.

    The response is gzipped JSON; `requests` decompresses transparently.
    Use `next_server_version(payload)` on the result to determine what
    to persist back into `StateCache.set_server_version()` for the
    next run.
    """
    params_base = {"deviceId": device_id}
    if not paginate:
        resp = _get_pull_sync(
            token,
            params={**params_base, "serverVersion": server_version},
        )
        if not resp.ok:
            if (
                fallback_full_on_500
                and resp.status_code == 500
                and server_version > const.PULL_SYNC_INITIAL_SERVER_VERSION
            ):
                log.warning(
                    "Migaku /pull-sync returned 500 at serverVersion=%d "
                    "(stale cursor after a push?) — retrying full refresh.",
                    server_version,
                )
                return pull_sync(
                    token,
                    device_id,
                    server_version=const.PULL_SYNC_INITIAL_SERVER_VERSION,
                    paginate=False,
                    fallback_full_on_500=False,
                )
            raise RuntimeError(
                f"Migaku /pull-sync failed ({resp.status_code}): {resp.text[:500]}"
            )
        return resp.json()

    current_sv = server_version
    pages = 0
    merged: dict[str, Any] = {}
    while pages < max_pages:
        resp = _get_pull_sync(
            token,
            params={**params_base, "serverVersion": current_sv},
        )
        if not resp.ok:
            if (
                fallback_full_on_500
                and resp.status_code == 500
                and server_version > const.PULL_SYNC_INITIAL_SERVER_VERSION
                and pages == 0
            ):
                log.warning(
                    "Migaku /pull-sync returned 500 at serverVersion=%d "
                    "(stale cursor after a push?) — retrying full refresh.",
                    server_version,
                )
                return pull_sync(
                    token,
                    device_id,
                    server_version=const.PULL_SYNC_INITIAL_SERVER_VERSION,
                    paginate=paginate,
                    max_pages=max_pages,
                    fallback_full_on_500=False,
                )
            raise RuntimeError(
                f"Migaku /pull-sync failed ({resp.status_code}): {resp.text[:500]}"
            )
        payload = resp.json()
        _merge_payload_page(merged, payload)
        pages += 1

        nxt = next_server_version(payload, previous=current_sv)
        if _all_arrays_empty(payload):
            break
        if nxt <= current_sv:
            log.warning(
                "/pull-sync pagination stopped early: non-empty page but "
                "serverVersion did not advance (%d).", current_sv
            )
            break
        current_sv = nxt

    if pages >= max_pages:
        log.warning(
            "/pull-sync hit max_pages=%d; returning partial merged payload. "
            "Increase max_pages if needed.", max_pages
        )
    return merged


def _merge_payload_page(merged: dict[str, Any], page: dict[str, Any]) -> None:
    """Merge one /pull-sync page into the running payload.

    List-valued keys append. Scalar/object keys keep the latest non-empty
    value. This mirrors how the extension walks serverVersion pages and
    accumulates entities.
    """
    for key, value in page.items():
        if isinstance(value, list):
            existing = merged.get(key)
            if not isinstance(existing, list):
                merged[key] = []
                existing = merged[key]
            existing.extend(value)
        elif value not in (None, "", {}):
            merged[key] = value


def _all_arrays_empty(payload: dict[str, Any]) -> bool:
    """True when every list field in the page is empty."""
    saw_list = False
    for value in payload.values():
        if isinstance(value, list):
            saw_list = True
            if value:
                return False
    return saw_list


def next_server_version(payload: dict[str, Any], previous: int = 0) -> int:
    """Find the highest `serverVersion`-equivalent in a /pull-sync response.

    Discovery order (most-trusted first):
      1. Top-level `serverVersion` (the field name the browser extension
         sends back on the next request) — checked first.
      2. Anything in `keyValue[]` whose `key` looks like a server-version
         marker (case-insensitive variants of "server_version" /
         "serverVersion" / "version").
      3. Fallback: the maximum `serverMod` across every list-shaped
         array in the response. The Go reference (migoku_api.go's
         `migakuSyncPayload`) treats `serverMod` as a per-entity
         monotonic counter; Migaku's clients use the max as the
         resume token.

    Returns `previous` if nothing matched (so an empty/no-op response
    keeps the existing cursor).
    """
    # 1. Top-level field (most reliable when present).
    for key in ("serverVersion", "server_version", "version"):
        v = payload.get(key)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)

    # 2. keyValue[] entries.
    for kv in payload.get("keyValue") or []:
        if not isinstance(kv, dict):
            continue
        key = (kv.get("key") or kv.get("name") or "").lower()
        if key in {"server_version", "serverversion", "version"}:
            for cand_key in ("value", "intValue", "data"):
                v = kv.get(cand_key)
                if isinstance(v, int):
                    return v
                if isinstance(v, str) and v.isdigit():
                    return int(v)

    # 3. Max serverMod across every array.
    best = previous
    for arr in payload.values():
        if not isinstance(arr, list):
            continue
        for item in arr:
            if not isinstance(item, dict):
                continue
            mod = item.get("serverMod")
            if isinstance(mod, int) and mod > best:
                best = mod
    return best


def list_words(
    token: AuthLike,
    device_id: str,
    language: str,
    server_version: int = const.PULL_SYNC_INITIAL_SERVER_VERSION,
) -> Iterable[Word]:
    """Yield Word objects projected from /pull-sync's `words[]` array.

    v1's `MigokuClient.list_words` paginated by status; v2 fetches the
    whole payload (status filtering happens client-side in the sync
    flow). Volume is fine — typically 1-2k rows, ~500 KB gzipped.

    Skips tombstoned (`del == 1`) entries and any whose `language`
    doesn't match (when set on the row — Migaku sometimes leaves it
    blank, in which case we trust the caller's filter).
    """
    payload = pull_sync(token, device_id, server_version=server_version)
    yield from words_from_payload(payload, language)


def words_from_payload(payload: dict[str, Any], language: str) -> Iterable[Word]:
    """Pure-function: project an already-fetched /pull-sync payload's
    `words[]` array into Word objects. Used by the sync flow to share
    one /pull-sync round-trip between list_words and compute_difficulty.
    """
    for raw in payload.get("words") or []:
        if raw.get("del"):
            continue
        row_lang = raw.get("language") or ""
        if language and row_lang and row_lang != language:
            continue
        yield MigakuEntity.word_from_raw(raw, language)


# ---------------------------------------------------------------------------
# Difficulty enrichment (v1 used migoku /api/v1/words/difficult; v2 computes
# locally from /pull-sync's `cards`, `cardWordRelations`, and `reviews`).
# ---------------------------------------------------------------------------

def compute_difficulty(
    payload: dict[str, Any],
    *,
    language: str,
    deck_id: str | None = None,
    min_reviews: int = 5,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """Compute fail-rate / total-reviews / failed-reviews per word from a
    /pull-sync payload.

    This is the v2 replacement for migoku's /api/v1/words/difficult.
    The new Migaku API does NOT expose a difficulty endpoint, but the
    raw data is in /pull-sync (which is *exactly* the data migoku was
    aggregating in-memory off the SRS SQLite). We just recreate the
    aggregation here.

    Aggregation key: (dictForm, secondary)
    --------------------------------------
    migoku's SQL grouped by ALL THREE of `(dictForm, secondary,
    partOfSpeech)`. v2 deliberately drops `partOfSpeech` from the key.

    Reason (Greg, 2026-05-07): Migaku stores `partOfSpeech` as a *list*
    on each word — observed across all 1500 words of his Mandarin
    course. Keying on it would splinter polysemous words like 行 into
    multiple aggregation buckets (`("行", "0", "v")` vs `("行", "0",
    "n")` vs `("行", "0", "v,n")` depending on how the list was
    serialised), which is not what we want in Notion. Notion shows
    ONE fail rate per word, so we aggregate to one bucket per
    (dictForm, secondary) and surface the union of POS values as a
    `parts_of_speech: list[str]` attribute on the row.

    Source-of-truth SQL from migoku/repository.go::GetDifficultWords
    (kept here for reference; v2 deliberately diverges on the key):

        SELECT
            w.dictForm, w.secondary, w.partOfSpeech, w.knownStatus,
            COUNT(r.id) AS total_reviews,
            SUM(CASE WHEN r.type = 1 THEN 1 ELSE 0 END) AS failed_reviews,
            ROUND(CAST(SUM(...failed...) AS FLOAT) / COUNT(r.id) * 100, 2) AS fail_rate
        FROM WordList w
        JOIN CardWordRelation cwr ON
              w.dictForm     = cwr.dictForm
          AND w.secondary    = cwr.secondary
          AND w.partOfSpeech = cwr.partOfSpeech    -- v2 drops this clause
        JOIN card   c ON cwr.cardId = c.id
        JOIN review r ON c.id      = r.cardId
        WHERE w.language = ?
          AND w.del = 0
          AND c.del = 0
          AND r.del = 0
          AND r.type IN (1, 2)        -- 1 = fail, 2 = pass; ignore 3+
          [AND c.deckId = ?]          -- only when deckId filter is set
        GROUP BY w.dictForm, w.secondary, w.partOfSpeech    -- v2: just (dictForm, secondary)
        HAVING total_reviews >= 5
        ORDER BY fail_rate DESC, total_reviews DESC
        LIMIT ?;

    Python translation outline:

        def _normalise_pos(value: Any) -> list[str]:
            # Migaku returns partOfSpeech as either a list[str] (modern)
            # or a single string (legacy / SQLite-derived). Normalise to
            # a deduped sorted list so the aggregation can union them
            # cleanly downstream.
            if value is None or value == "":
                return []
            if isinstance(value, (list, tuple)):
                return [str(p).strip() for p in value if p and str(p).strip()]
            # Legacy string. Some Migaku exports comma-joined multiple
            # POS values into one string (e.g. "v,n"); split defensively.
            return [p.strip() for p in str(value).split(",") if p.strip()]

        # 1. Index card_id -> card (filter c.del == 0 and optional deckId).
        cards_by_id = {
            c["id"]: c for c in (payload.get("cards") or [])
            if not c.get("del") and (deck_id is None or c.get("deckId") == deck_id)
        }

        # 2. Index card_id -> list of (dictForm, secondary) keys.
        #    NOTE: partOfSpeech intentionally NOT in the key (see header).
        word_keys_for_card: dict[str, list[tuple[str, str]]] = {}
        for cwr in payload.get("cardWordRelations") or []:
            cid = cwr.get("cardId")
            if cid not in cards_by_id:
                continue
            word_keys_for_card.setdefault(cid, []).append((
                cwr.get("dictForm", ""),
                cwr.get("secondary", ""),
            ))

        # 3. Walk reviews; for each, attribute it to every word the card
        #    represents. Bucket = [total, failed].
        totals: dict[tuple[str, str], list[int]] = {}
        for r in payload.get("reviews") or []:
            if r.get("del") or r.get("type") not in (1, 2):
                continue
            cid = r.get("cardId")
            for wkey in word_keys_for_card.get(cid, ()):
                bucket = totals.setdefault(wkey, [0, 0])
                bucket[0] += 1
                if r.get("type") == 1:
                    bucket[1] += 1

        # 4. Build a (dictForm, secondary) -> {known_status, parts_of_speech_set}
        #    index from the words[] array so we can attach POS+status to
        #    each aggregated bucket. POS values from MULTIPLE underlying
        #    word rows for the same key get unioned together.
        word_attrs: dict[tuple[str, str], dict[str, Any]] = {}
        for w in payload.get("words") or []:
            if w.get("del"):
                continue
            if language and w.get("language") and w.get("language") != language:
                continue
            wkey = (w.get("dictForm", ""), w.get("secondary", ""))
            attrs = word_attrs.setdefault(wkey, {
                "known_status": w.get("knownStatus"),
                "pos_set": set(),
            })
            for p in _normalise_pos(w.get("partOfSpeech")):
                attrs["pos_set"].add(p)
            # If multiple word rows exist for the same key (rare), keep
            # the first-seen knownStatus; deeper merging isn't worth it.

        # 5. Project + filter (min_reviews threshold).
        out = []
        for wkey, (total, failed) in totals.items():
            if total < min_reviews:
                continue
            attrs = word_attrs.get(wkey)
            if attrs is None:
                continue
            fail_rate = round(failed / total * 100, 2) if total else 0.0
            out.append({
                "dictForm":         wkey[0],
                "secondary":        wkey[1],
                "parts_of_speech":  sorted(attrs["pos_set"]),
                "knownStatus":      attrs["known_status"],
                "total_reviews":    total,
                "failed_reviews":   failed,
                "fail_rate":        fail_rate,
            })

        # 6. Sort + truncate. Mirror migoku's ORDER BY exactly.
        out.sort(key=lambda d: (-d["fail_rate"], -d["total_reviews"]))
        return out[:limit]

    Notes:
      - Output schema differs from v1's `/api/v1/words/difficult` in
        ONE field: v1 returned `partOfSpeech: <single string>` (the
        first one to win the SQL JOIN); v2 returns
        `parts_of_speech: list[str]` (the union). The downstream
        merger in v2's `sync` should comma-join this list into the
        existing Notion `Part of speech` column — see
        notion_client.build_properties() which already handles list
        input defensively.
      - Edge case: a card relating to multiple words (each review
        counted against each word — matches the SQL JOIN semantics
        with the relaxed key).
      - Tombstoned cards/reviews/words filtered by `del` flag.
      - The `min_reviews=5` threshold matches migoku's `HAVING
        total_reviews >= 5`. Greg's v1↔v2 validation set will exclude
        rows below that threshold anyway, so it's also the right
        comparison point.

    Validation methodology: see ../../../tests/v1_v2_math_validation.md
    for the v1↔v2 fail-rate cross-check Greg signed off on (including
    the "Why we relaxed the key" preamble that explains why the v2 row
    count and POS shape diverge from v1).
    """
    cards_by_id = {
        c["id"]: c for c in (payload.get("cards") or [])
        if isinstance(c, dict) and not c.get("del")
        and (deck_id is None or c.get("deckId") == deck_id)
    }

    word_keys_for_card: dict[Any, list[tuple[str, str]]] = {}
    for cwr in payload.get("cardWordRelations") or []:
        if not isinstance(cwr, dict):
            continue
        cid = cwr.get("cardId")
        if cid not in cards_by_id:
            continue
        word_keys_for_card.setdefault(cid, []).append((
            cwr.get("dictForm", "") or "",
            cwr.get("secondary", "") or "",
        ))

    totals: dict[tuple[str, str], list[int]] = {}
    for r in payload.get("reviews") or []:
        if not isinstance(r, dict) or r.get("del"):
            continue
        if r.get("type") not in (1, 2):
            continue
        cid = r.get("cardId")
        for wkey in word_keys_for_card.get(cid, ()):
            bucket = totals.setdefault(wkey, [0, 0])
            bucket[0] += 1
            if r.get("type") == 1:
                bucket[1] += 1

    word_attrs: dict[tuple[str, str], dict[str, Any]] = {}
    for w in payload.get("words") or []:
        if not isinstance(w, dict) or w.get("del"):
            continue
        row_lang = w.get("language") or ""
        if language and row_lang and row_lang != language:
            continue
        wkey = (w.get("dictForm", "") or "", w.get("secondary", "") or "")
        attrs = word_attrs.setdefault(wkey, {
            "known_status": w.get("knownStatus"),
            "pos_set": set(),
        })
        for p in _normalise_pos(w.get("partOfSpeech")):
            attrs["pos_set"].add(p)

    out: list[dict[str, Any]] = []
    for wkey, (total, failed) in totals.items():
        if total < min_reviews:
            continue
        attrs = word_attrs.get(wkey)
        if attrs is None:
            continue
        fail_rate = round(failed / total * 100, 2) if total else 0.0
        out.append({
            "dictForm":        wkey[0],
            "secondary":       wkey[1],
            "parts_of_speech": sorted(attrs["pos_set"]),
            "knownStatus":     attrs["known_status"],
            "total_reviews":   total,
            "failed_reviews":  failed,
            "fail_rate":       fail_rate,
        })

    out.sort(key=lambda d: (-d["fail_rate"], -d["total_reviews"]))
    return out[:limit]


def _normalise_pos(value: Any) -> list[str]:
    """Normalise Migaku's `partOfSpeech` field (list, comma-string, or None)
    into a clean list of stripped non-empty strings.
    """
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(p).strip() for p in value if p is not None and str(p).strip()]
    return [p.strip() for p in str(value).split(",") if p.strip()]


def list_difficult_words(
    token: AuthLike,
    device_id: str,
    language: str,
    limit: int = 2000,
    *,
    deck_id: str | None = None,
) -> list[dict[str, Any]]:
    """Pull a fresh /pull-sync and run compute_difficulty() on it.

    Convenience for one-shot callers. The sync flow makes its own
    /pull-sync round-trip and reuses the payload — call
    compute_difficulty(payload, ...) directly there to avoid a second
    network round-trip.
    """
    payload = pull_sync(token, device_id, server_version=0)
    return compute_difficulty(
        payload, language=language, deck_id=deck_id, limit=limit,
    )

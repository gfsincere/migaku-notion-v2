"""Write side: POST core-server.migaku.com/push/enqueue.

The new capability v2 adds vs v1: writing card / word state changes back
to Migaku. The v1 sync was strictly Migaku → Notion; v2 will eventually
support both directions, and the same /push/enqueue endpoint is used for:

  - Setting word status (knownStatus / tracked).
  - Creating new cards (with images + audio attached via files.py).
  - Updating existing cards.

Endpoint (from HAR entries #01, #20, #22):
    POST https://core-server.migaku.com/push/enqueue
        ?deviceId=<32-hex>
        &deviceVersion=<int>          # client's known serverVersion +1?
    Content-Type: application/octet-stream
    Authorization: Bearer <firebase id_token>

    body: a `migakuSyncPayload` (decks / cardTypes / cards /
          cardWordRelations / vacations / reviews / words / config /
          keyValue / learningMaterials / lessons / reviewHistory /
          libraryItems). All arrays must be present; populate just the
          ones you're changing.

Response:
    202 Accepted, body:
        {
          "enqueued": true,
          "messageId": "...",
          "orderingKey": "<userId>",
          "receivedAt": <epoch_ms>
        }

Body framing — Greg-approved discovery sequence:

  Step 1 (try first): plain JSON.
      Content-Type: application/json
      body: json.dumps(payload).encode("utf-8")
      No new dependencies. If Migaku accepts this (202 + the `enqueued`
      response shape), we're done — keep it and move on.

  Step 2 (fallback if step 1 4xx's): zstd-compressed JSON.
      Add `zstandard` to requirements.txt (pin like `zstandard>=0.22,<1`),
      then:
          Content-Type: application/octet-stream
          Content-Encoding: zstd
          body: zstandard.ZstdCompressor().compress(json.dumps(payload).encode())
      Matches the wire shape observed in the HAR (octet-stream with what
      looks like a zstd magic prefix). Only adopt this once step 1 has
      been *demonstrated* not to work; we don't want zstd as a dep on
      speculation.

The Go reference is `migoku/migaku_api.go::PushSync` (which targeted the
older Cloud-Run-direct URL but used the same payload shape and Bearer
auth). Word-status PATCH semantics are in
`migoku/word_status.go::setWordStatusItems` — that's where to find the
exact field set Migaku expects when bumping `knownStatus` for a word.

The Go reference is `migoku/migaku_api.go::PushSync` (which targeted the
older Cloud-Run-direct URL but used the same payload shape and Bearer
auth). Word-status PATCH semantics are in
`migoku/word_status.go::setWordStatusItems` — that's where to find the
exact field set Migaku expects when bumping `knownStatus` for a word.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Iterable

import requests

from . import auth, const  # noqa: F401

log = logging.getLogger("migaku-notion")

_PUSH_ENQUEUE_TIMEOUT = 120
_PUSH_ENQUEUE_RETRIES = 3


def _encode_lzo(payload: bytes) -> bytes:
    """Encode payload in Migaku's LZO-framed wire format.

    HAR frames start with b"LZ" then 4-byte little-endian original length.
    The remaining bytes are minilzo-compressed payload.
    """
    try:
        import minilzo
    except Exception:
        # Keep a graceful fallback for environments without minilzo.
        return payload
    compressed = minilzo.compress(payload)
    return b"LZ" + len(payload).to_bytes(4, byteorder="little", signed=False) + compressed


def empty_payload() -> dict[str, list[Any]]:
    """Return a fresh `migakuSyncPayload` with every array empty.

    The browser extension always sends a complete payload — even when only
    one array is non-empty. Mirrors `migakuSyncPayload` in
    migoku/migaku_api.go.
    """
    return {
        "decks":             [],
        "cardTypes":         [],
        "cards":             [],
        "cardWordRelations": [],
        "vacations":         [],
        "reviews":           [],
        "words":             [],
        "config":            None,
        "keyValue":          [],
        "learningMaterials": [],
        "lessons":           [],
        "reviewHistory":     [],
        "libraryItems":      [],
    }


def push_enqueue(
    token: auth.FirebaseAuthToken,
    device_id: str,
    device_version: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Send a single /push/enqueue and return the parsed response dict.

    Implementation outline (start here — plain JSON, no extra deps):
        token = auth.ensure_fresh(token)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        resp = requests.post(
            const.PUSH_ENQUEUE_URL,
            params={"deviceId": device_id, "deviceVersion": device_version},
            data=body,
            headers={
                "Authorization": f"Bearer {token.id_token}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    Only if the call above 4xx's, fall back to zstd:
        # Add zstandard to requirements.txt only when this branch triggers.
        import zstandard
        body = zstandard.ZstdCompressor().compress(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        # then send with Content-Type application/octet-stream and
        # Content-Encoding: zstd, as documented in the module docstring.
    """
    token = auth.ensure_fresh(token)
    params = {"deviceId": device_id, "deviceVersion": int(device_version)}
    body_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body_lzo = _encode_lzo(body_json)

    def _post(version: int) -> requests.Response:
        last_exc: BaseException | None = None
        for attempt in range(_PUSH_ENQUEUE_RETRIES):
            try:
                return requests.post(
                    const.PUSH_ENQUEUE_URL,
                    params={"deviceId": device_id, "deviceVersion": int(version)},
                    data=body_lzo,
                    headers={
                        "Authorization": f"Bearer {token.id_token}",
                        "Content-Type": "application/octet-stream",
                        # Required by Migaku's current core-server write path.
                        "x-content-encoding": "lzo",
                    },
                    timeout=_PUSH_ENQUEUE_TIMEOUT,
                )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_exc = exc
                if attempt + 1 >= _PUSH_ENQUEUE_RETRIES:
                    break
                wait = min(2 ** attempt, 10)
                log.warning(
                    "Migaku /push/enqueue network error (attempt %d/%d); "
                    "retrying in %ds: %s",
                    attempt + 1,
                    _PUSH_ENQUEUE_RETRIES,
                    wait,
                    exc,
                )
                time.sleep(wait)
        assert last_exc is not None
        raise RuntimeError(
            f"Migaku /push/enqueue timed out after {_PUSH_ENQUEUE_RETRIES} attempts "
            f"({type(last_exc).__name__})"
        ) from last_exc

    resp = _post(int(device_version))
    if resp.ok:
        return resp.json()

    # Drift can happen when another client pushes first; retry once with server hint.
    if resp.status_code == 409:
        m = re.search(r"expected=(\d+),\s*got=(\d+)", resp.text or "")
        if m:
            expected = int(m.group(1))
            resp2 = _post(expected)
            if resp2.ok:
                return resp2.json()
            raise RuntimeError(
                f"Migaku /push/enqueue failed after VERSION_MISMATCH retry "
                f"({resp2.status_code}): {resp2.text[:500]}"
            )

    raise RuntimeError(
        f"Migaku /push/enqueue failed ({resp.status_code}): {resp.text[:500]}"
    )


def set_word_status(
    token: auth.FirebaseAuthToken,
    device_id: str,
    device_version: int,
    *,
    word_text: str,
    secondary: str,
    part_of_speech: str,
    language: str,
    status: str,        # "KNOWN" | "LEARNING" | "UNKNOWN" | "IGNORED" | "TRACKED"
) -> dict[str, Any]:
    """Convenience: change a single word's status.

    The Go reference is migoku/word_status.go::setWordStatusItems —
    specifically the bit that builds the `payload` dict before calling
    PushSync. Key fields per word: dictForm, secondary, partOfSpeech,
    language, knownStatus, tracked, mod (UnixMilli now), serverMod,
    hasCard.

    For status="TRACKED": knownStatus="UNKNOWN" + tracked=true (bizarre
    but matches Migaku's own semantics).

    Implementation outline:
        body = empty_payload()
        body["words"] = [{
            "dictForm":     word_text,
            "secondary":    secondary,
            "partOfSpeech": part_of_speech,
            "language":     language,
            "knownStatus":  status if status != "TRACKED" else "UNKNOWN",
            "tracked":      status == "TRACKED",
            "mod":          int(time.time() * 1000),
            "serverMod":    -1,
            "del":          0,
        }]
        return push_enqueue(token, device_id, device_version, body)
    """
    status_up = status.strip().upper()
    if status_up not in {"KNOWN", "LEARNING", "UNKNOWN", "IGNORED", "TRACKED"}:
        raise ValueError(f"Unsupported status: {status}")

    known_status = "UNKNOWN" if status_up == "TRACKED" else status_up
    tracked = status_up == "TRACKED"
    now_ms = int(time.time() * 1000)

    body = empty_payload()
    body["words"] = [{
        "dictForm": word_text,
        "secondary": secondary,
        "partOfSpeech": part_of_speech,
        "language": language,
        "knownStatus": known_status,
        "tracked": tracked,
        "mod": now_ms,
        "serverMod": -1,
        "del": 0,
        "hasCard": False,
    }]
    return push_enqueue(token, device_id, device_version, body)


def push_words(
    token: auth.FirebaseAuthToken,
    device_id: str,
    device_version: int,
    word_records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Bulk version of set_word_status for arbitrary word_record dicts.

    Each `word_record` should be the *full* dict Migaku expects (matching
    the keys of MigakuWord in migoku_api.go). Use this once we're round-
    tripping rows fetched from /pull-sync — just modify the relevant
    fields and pass the rest through unchanged.
    """
    body = empty_payload()
    body["words"] = list(word_records)
    return push_enqueue(token, device_id, device_version, body)

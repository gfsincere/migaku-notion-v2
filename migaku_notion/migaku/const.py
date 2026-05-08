"""Hardcoded URLs and the Migaku Firebase API key.

`MIGAKU_API_KEY` is the public Firebase Web API key shipped with every
Migaku client (browser extension, mobile app). It's not a secret — it just
identifies the Firebase project. Lifted verbatim from migoku/migaku_api.go
where it has been stable since at least 2024.
"""
from __future__ import annotations


MIGAKU_API_KEY = "AIzaSyDZvwYKYTsQoZkf3oKsfIQ4ykuy2GZAiH8"


# --- Firebase auth ---------------------------------------------------------
# Both endpoints take `?key=<MIGAKU_API_KEY>` as a query parameter.
FIREBASE_SIGN_IN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
)
FIREBASE_REFRESH_URL = "https://securetoken.googleapis.com/v1/token"


# --- core-server (read + write of the Migaku sync payload) -----------------
# The legacy `migakuSyncServerURL` in the Go code
# (`https://core-server-mohegkboza-uc.a.run.app`) is the *internal* Cloud Run
# revision URL. The browser-facing host visible in the HAR is the friendly
# alias below. They both terminate at the same backend, but the friendly
# alias is the one current Migaku clients hit.
CORE_SERVER_BASE = "https://core-server.migaku.com"
PULL_SYNC_URL = f"{CORE_SERVER_BASE}/pull-sync"
PUSH_ENQUEUE_URL = f"{CORE_SERVER_BASE}/push/enqueue"


# --- file-sync-worker (media upload) ---------------------------------------
FILE_SYNC_WORKER_BASE = "https://file-sync-worker-api.migaku.com"
FILE_SYNC_DATA_PREFIX = f"{FILE_SYNC_WORKER_BASE}/data/SRSMEDIA"


# --- ai-worker (translation, definition, nuance) ---------------------------
# Optional. Same OpenAI-style chat surface; v2 currently doesn't depend on
# it but we expose the URL here for future commands.
AI_WORKER_COMPLETE_SYNC_URL = "https://ai-worker.migaku.com/complete-sync"


# --- /push/enqueue body framing --------------------------------------------
# Observed in HAR (preply-migaku/lesson_samples/migaku-card-creator.har):
#   - request Content-Type: application/octet-stream
#   - first bytes look like zstd magic ("LZí"-ish) followed by the JSON
#     `migakuSyncPayload`. Migaku's browser extension presumably zstd-
#     compresses before sending.
# When wiring this up, try in this order until one returns 202:
#   1. Plain JSON body (Content-Type: application/json) — simplest.
#   2. JSON body, Content-Encoding: gzip.
#   3. JSON body, Content-Encoding: zstd (`pip install zstandard`).
PUSH_ENQUEUE_CONTENT_TYPE = "application/octet-stream"


# --- /pull-sync pagination -------------------------------------------------
# /pull-sync uses `serverVersion` (an int that monotonically increases with
# each push). The browser extension keeps polling with the latest known
# `serverVersion` and stops when the response arrays are all empty.
PULL_SYNC_INITIAL_SERVER_VERSION = 0

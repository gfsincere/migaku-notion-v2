"""Direct talk to Migaku's HTTP API. Replaces the v1 migoku Go server.

This package is the v2 successor to the legacy `migoku` Go service. It
talks straight to:

  - https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword
       Firebase email-password login (returns refresh + idToken).
  - https://securetoken.googleapis.com/v1/token?key=<MIGAKU_API_KEY>
       Refreshes the short-lived idToken from the long-lived refresh token.
  - https://core-server.migaku.com/pull-sync?serverVersion=N&deviceId=H
       Pulls everything new since `serverVersion`. JSON, gzipped on the
       wire. Same shape as the v1 `migakuSyncPayload`, plus `wordHistory`
       and `libraryItems`.
  - https://core-server.migaku.com/push/enqueue?deviceId=H&deviceVersion=N
       Pushes a `migakuSyncPayload` (decks/words/cards/...) for write-back.
       Body is `application/octet-stream` (looks compressed in the HAR;
       expected to be the JSON payload, possibly zstd-compressed — TBD
       once we replay against a live token).
  - https://file-sync-worker-api.migaku.com/data/SRSMEDIA/<urlencoded-name>
       Uploads media bytes (image/audio) for a card. Returns
       `{filePath: "<userId>/SRSMEDIA/<uuid>_<name>"}` — the value the
       enqueue payload references in `card.audio` / `card.image` fields.

All four endpoint modules are STUBS in this scaffold. Each one's docstring
spells out the request/response shapes from the HAR so wiring them up is
mostly mechanical.

The single source of truth for the API contract is:
    <preply-migaku>/lesson_samples/migaku-card-creator.har
which can be re-inspected at any time with the local `inspect_har.py`
script (copied into this repo for convenience).
"""

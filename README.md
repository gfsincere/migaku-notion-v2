# migaku-notion-v2

Mirror your [Migaku](https://migaku.com) vocabulary into a Notion database (or keep a local cache only) so you can quiz, filter, and export your real word list. Pure Python: no Docker, no [migoku](https://github.com/khatibomar/migoku) server. This is the spiritual successor to the older [migaku-notion](https://github.com/gfsincere/migaku-notion) fork: same CLI shape and cache idea, more functionality (direct Migaku API, dictionary and frequency enrichment, optional Notion).

## Install

```powershell
cd <your-projects-dir>
git clone https://github.com/gfsincere/migaku-notion-v2.git
cd migaku-notion-v2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m migaku_notion setup
```

You need Python 3.11+, a Migaku account, and (only if you want Notion) a Notion internal integration and database. `setup` can skip Notion; then use `sync --no-notion` or leave `NOTION_TOKEN` / `NOTION_DATABASE_ID` unset.

## Upgrade from migaku-notion

If you used the older repo under `migaku-notion/`:

1. Clone and install v2 as above (separate folder and venv).
2. Copy `state.db` from `migaku-notion/sync/` into this repo root (same filename: `state.db`).
3. Copy `migaku-notion/sync/.env` here as `.env`, then replace Migoku vars with Migaku: set `MIGAKU_EMAIL` / `MIGAKU_PASSWORD` (or run `python -m migaku_notion login` to write `MIGAKU_REFRESH_TOKEN`), remove `MIGOKU_*`. Keep `NOTION_TOKEN`, `NOTION_DATABASE_ID`, and `SYNC_*` if you use Notion.
4. Run `python -m migaku_notion status` then `python -m migaku_notion sync --dry-run`, then `python -m migaku_notion sync`.

Full detail: [MIGRATION-FROM-V1.md](./MIGRATION-FROM-V1.md).

## Why v2

- Talks to Migaku over HTTPS (`core-server.migaku.com` `/pull-sync`, Firebase auth) instead of a local migoku Go server.
- Enrichment from Migaku public dictionary SQLite plus per-language `frequency.db`; `pypinyin` only when the dict misses a term.
- Fail rate and review counts computed locally from the pull payload (`cards`, `cardWordRelations`, `reviews`), not migoku's difficult-words endpoint.
- Notion is optional: omit credentials or pass `sync --no-notion` to update `state.db` only.
- Planned: `/push/enqueue` and SRSMEDIA uploads for writing cards and media back to Migaku (see `migaku_notion/migaku/push.py` and `files.py`; pair with your own HAR captures of those endpoints when wiring writes).

Credit: the data model and early reverse engineering came from [migoku](https://github.com/khatibomar/migoku); v2 does not run it.

## Architecture

```
                       Migaku account
                       (your data)
                             │
                             │  1. POST identitytoolkit/v1/accounts:signInWithPassword
                             │     → refreshToken + idToken
                             │  2. POST securetoken/v1/token  (refresh on each run)
                             ▼
                       Firebase Auth
                             │
                             │  Authorization: Bearer <idToken>
                             │
              ┌──────────────┼─────────────────────────┐
              ▼              ▼                         ▼
   GET /pull-sync    POST /push/enqueue       PUT /data/SRSMEDIA/<file>
   (core-server)     (core-server)            (file-sync-worker-api)
   read vocabulary   write cards (planned)    upload audio/images (planned)
              │              │                         │
              └──────────────┼─────────────────────────┘
                             ▼
                  migaku-notion-v2 (Python)
                             │  ┌──────────────────────────────────┐
                             │  │  ~/.migaku-notion-v2/dicts/        │
                             │  │  dictionary + frequency SQLite   │
                             │  └──────────────────────────────────┘
                             │
                             │  pull-sync (paginate serverVersion)
                             │  enrich from dict + frequency.db
                             │  compute fail rates from cards / relations / reviews
                             │  diff against state.db
                             │  upsert optional Notion DB, or cache-only
                             ▼
                Notion (optional)  +  state.db
```

`POST /push/enqueue` and SRSMEDIA upload are the next wiring targets; read path and Notion or local-only sync are what works today.

## migoku vs v2

[migoku](https://github.com/khatibomar/migoku) is community Go code that logs into Migaku, pulls the SRS-style payload, and exposes it over `localhost` REST. This repo talks to the same conceptual data without running migoku.

| Aspect | migoku | v2 (this repo) |
|--------|--------|----------------|
| Runtime | Go binary you run locally (often via Docker) | Python package only |
| Migaku I/O | Download / cache SRS sync data, serve REST on localhost | HTTPS to `core-server.migaku.com`, Firebase auth, `GET /pull-sync` |
| Read shape | SQLite + HTTP queries your script calls | JSON sync payload; client paginates `serverVersion` until caught up |
| Mandarin readings / gloss | Not the main REST story | Migaku public dict SQLite + `frequency.db`; `pypinyin` if dict misses |
| Fail rate / review stats | SQL and endpoints over the local DB | Aggregated in Python from `cards`, `cardWordRelations`, `reviews` in the pull payload |
| Notion | Not migoku's job; other tools sat on top | Built-in optional Notion upsert + same `state.db` diff idea |
| Write back to Migaku | Present in Go reference; most bridges stayed read-only | Read path shipped; `/push/enqueue` + media upload still to implement |

## Commands

```powershell
python -m migaku_notion status
python -m migaku_notion sync
python -m migaku_notion sync --full-refresh
python -m migaku_notion sync --no-notion
python -m migaku_notion sync --dry-run
python -m migaku_notion rebuild-cache
python -m migaku_notion chars
python -m migaku_notion export --csv out.csv
python -m migaku_notion export --xlsx out.xlsx
```

Re-runs diff against `state.db` so Notion only gets PATCHes when tracked fields change. On the first v2 sync, dict meaning is written only into rows whose Meaning is blank (unless `--no-dict-meanings`); after that, Meaning is not overwritten.

`MIGAKU_DEVICE_ID` in `.env` is a stable 32-hex device id for Migaku; changing it behaves like a new client (full pull next time).

## Notion schema

Same database properties as the older migaku-notion tool, plus columns `Frequency` (number) and `Example` (rich text). `setup` can add missing columns on an existing DB.

| Property | Type | Notes |
|----------|------|--------|
| Word | Title | dictForm |
| Pinyin | Rich text | From dict per sense; else pypinyin for zh |
| Meaning | Rich text | First sync fills blanks only |
| Example | Rich text | v2 |
| Pinyin (numeric) | Rich text | zh |
| Status | Select | KNOWN, LEARNING, etc. |
| Frequency | Number | v2, from frequency.db |
| Fail rate % | Number | Local from reviews |
| Total reviews | Number | |
| Failed reviews | Number | |
| Part of speech | Rich text | |
| Language | Select | e.g. zh, ja |
| Last synced | Date | |
| Migaku key | Rich text | lang\|dictForm\|secondary |
| Sense # | Rich text | zh homonym index |

Dictionary catalog: [index2.json](https://migaku-public-data.migaku.com/dicts/index2.json). Cached under `~/.migaku-notion-v2/dicts/`.

## Next milestones

Push and media to Migaku (`push_enqueue`, SRSMEDIA upload). Optional: Notion list import vs Migaku gap and enqueue. Optional: Firestore live sync.

## Credits

- [khatibomar/migoku](https://github.com/khatibomar/migoku)
- [Migaku](https://migaku.com)
- [pypinyin](https://github.com/mozillazg/python-pinyin)
- [Notion](https://notion.so)

## Support

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/blacktonystark)

## License

MIT.

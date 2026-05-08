# Migrating from migaku-notion v1 to v2

If you're running v1 today and want to move to v2, this is the path.
Should take ~5 minutes once v2's Migaku-side wiring is shipped (until
then, run them side-by-side with v1 doing the actual sync).

## TL;DR

```powershell
# 1. Clone v2 alongside v1 and bootstrap
git clone https://github.com/gfsincere/migaku-notion-v2.git
cd migaku-notion-v2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Reuse v1 state and config
copy ..\migaku-notion\sync\state.db .\state.db
copy ..\migaku-notion\sync\.env     .\.env       # then strip MIGOKU_* lines (see below)

# 3. Verify
python -m migaku_notion status
python -m migaku_notion sync --dry-run

# 4. Cut over
python -m migaku_notion sync                       # for real
cd ..\migaku-notion; docker compose down   # retire v1 (compose at fork repo root)
```

## What changes


|                        | **v1**                                             | **v2**                                                          |
| ---------------------- | -------------------------------------------------- | --------------------------------------------------------------- |
| **Runtime deps**       | Python + Go (Docker)                               | Python only                                                     |
| **Migaku transport**   | `localhost:8080` REST (migoku Go server)           | direct HTTPS to `core-server.migaku.com`                        |
| **Auth**               | migoku-derived `X-Api-Key` header                  | Firebase `Authorization: Bearer <id_token>`                     |
| **Read endpoint**      | `/api/v1/words?status=...&page=N` (paginated)      | `/pull-sync?serverVersion=N` (single payload)                   |
| **Difficulty data**    | `/api/v1/words/difficult` (migoku-side derivation) | computed locally from `/pull-sync` reviews (or skipped in v2.0) |
| **Container**          | `docker compose up -d` for migoku                  | none                                                            |
| **Languages in stack** | Python + Go                                        | Python                                                          |
| **Push to Migaku?**    | ❌ no (read-only)                                   | ✅ yes (new — `/push/enqueue`)                                   |
| **Audio upload?**      | ❌ no                                               | ✅ yes (new — `file-sync-worker-api`)                            |
| **Real-time sync?**    | ❌ no                                               | 🔜 stretch goal (Firestore channels)                            |


## What stays the same

- **The Notion database.** Same workspace, same database id, same schema
(Word / Pinyin / Meaning / Pinyin (numeric) / Status / Fail rate % /
Total reviews / Failed reviews / Part of speech / Language / Last
synced / Migaku key / Sense #). v2 doesn't touch it during migration.
- **The local `state.db` cache.** Schema is byte-for-byte identical.
Copy the file across and v2 reads it unchanged — no `rebuild-cache`
needed.
- **The `.env` keys for Notion.** `NOTION_TOKEN` and `NOTION_DATABASE_ID`
are the same; copy them straight across.
- **The `.env` keys for sync defaults.** `SYNC_LANG`, `SYNC_STATUS`,
`SYNC_DIFFICULT_LIMIT` all carry over.
- **The CLI surface.** Every v1 subcommand (`sync`, `rebuild-cache`,
`login`, `status`, `chars`, `setup`, `export`) exists in v2 with the
same name and the same flags. Only the invocation prefix changes
(`python sync.py X` → `python -m migaku_notion X`).
- **The "Meaning is never overwritten" rule.** Carries over verbatim.
- **The `Migaku key` dedup format** (`<lang>|<dictForm>|<secondary>`)
carries over verbatim — that's why `state.db` is portable.

## Step-by-step migration

### Step A — Install v2 alongside v1

v2 lives in a separate folder, separate venv. v1 keeps working; you can
run both side-by-side until you're confident v2 produces the same diffs.

```powershell
cd <your-projects-dir>
git clone https://github.com/gfsincere/migaku-notion-v2.git
cd migaku-notion-v2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Step B — Carry your `state.db` across

Schema is identical between v1 and v2, so just copy the file:

```powershell
copy ..\migaku-notion\sync\state.db .\state.db
```

(Linux/macOS: `cp ../migaku-notion/sync/state.db ./state.db`)

You can also skip this step and let v2's first sync bootstrap from
Notion, but the copy is faster and avoids hitting Notion's rate limit
for ~1500 page reads.

### Step C — Carry your `.env` across

```powershell
copy ..\migaku-notion\sync\.env .\.env
```

Then open `.env` in an editor and **delete these v1-only keys** (v2
ignores them, but they'll just clutter the file):

```dotenv
MIGOKU_URL=...           # remove
MIGOKU_API_KEY=...       # remove
```

`MIGAKU_EMAIL` and `MIGAKU_PASSWORD` carry over (v2 uses them for the
Firebase login). All `NOTION_*` and `SYNC_*` keys are unchanged.

### Step D — Verify

```powershell
python -m migaku_notion status
```

This should report:

- Migaku connectivity (when wired): `core-server.migaku.com OK`
- Notion: token + database id present
- local cache: same row count as v1's `state.db`

### Step E — Dry-run

```powershell
python -m migaku_notion sync --dry-run
```

If v1 just ran a sync, the existing v1 fields will report
`created=0 updated=0 unchanged=N`. Expect a moderate `updated=N` if you're
doing this on a typical v1-populated state.db, because v2 now writes
`Pinyin` from Migaku's dict (correct reading per Sense #) instead of
`pypinyin` and adds `Frequency` + `Example`. See "Improvements" below
before being alarmed.

### Step F — Real sync

```powershell
python -m migaku_notion sync
```

Same shape as v1's run. On the very first v2 run against your existing
Notion DB, expect:

- `Pinyin` to update on rows where `pypinyin` previously picked the wrong
reading for a homonym (e.g. 行 with `Sense # = 1` flips from `xíng` to
`háng`).
- `Meaning` to fill in *only* on rows where it's currently blank
(Greg-approved one-time auto-populate from Migaku's dict). Pass
`--no-dict-meanings` if you'd rather keep the v1 "always blank,
Notion AI fills it" workflow.
- `Frequency` (new column) to populate with 1-5 stars.
- `Example` (new column) to populate from the dict where available.

From sync #2 onward, `Meaning` is left strictly alone (matches v1).

### Step G — Retire v1

Once v2 has run cleanly at least once:

```powershell
cd ..\migaku-notion; docker compose down
```

You can keep the v1 folder around until the next major Migaku change
(or just `git status`-check it and delete it). v2 doesn't depend on v1
in any way.

## Rollback plan

If anything goes wrong with v2, falling back is trivial:

1. v2 has its own `state.db` (you copied a separate file in Step B). It
  does not touch v1's. Either copy survives independently.
2. The Notion database is shared, but v2's diff-aware sync means it
  only writes rows that genuinely changed. If you stop v2 mid-run,
   anything it had already written is consistent with its own
   `state.db`. Re-running v1 against the same Notion DB just sees those
   writes as "matches my cache, skip."
3. Worst case, run `python sync.py rebuild-cache` in v1 to re-pull
  ground truth from Notion, and resume on v1.

So: keep v1 around until you've done at least one full sync cycle in v2.

## Known v2 limitations vs v1

For the **read sync** (the only thing v1 did): nothing.

The one open question is fail-rate enrichment. v1 used `migoku /api/v1/words/difficult`, which doesn't have a direct equivalent in
Migaku's HTTP API. v2 instead computes fail-rate locally from
`/pull-sync`'s `cards` + `cardWordRelations` + `reviews` arrays — the
exact same data migoku was aggregating server-side. The algorithm is a
direct Python translation of `repository.go::GetDifficultWords`'s SQL
aggregation; see
`[migaku_notion.migaku.pull.compute_difficulty()](./migaku_notion/migaku/pull.py)`
for the algorithm spelled out.

If `compute_difficulty()` lands later than the rest of the sync, the
"Fail rate %" / "Total reviews" / "Failed reviews" Notion columns will
simply remain at their v1 values until the first v2 sync that includes
the computation. Nothing overwrites them with blanks in the meantime.

**Math correctness check.** v1 and v2 should produce identical
fail-rate output at the `(dictForm, secondary)` level (modulo float
rounding). The methodology for proving that — and the explicit pass
criteria, including Greg's instruction to exclude words with no
review history from the comparison — is written up in
`[tests/v1_v2_math_validation.md](./tests/v1_v2_math_validation.md)`.
Run that the first time `compute_difficulty()` is wired, before you
trust any v2 fail-rate numbers in production.

**Visible improvements (not regressions).**

1. **Part of speech** may now show comma-separated values like `"v, n"`
  for polysemous words (e.g. 行) where v1 only ever showed one. Migaku
   stores POS as a list per word; v1's SQL JOIN flattened that down to
   whichever single string SQLite happened to retain, while v2's
   `compute_difficulty()` keeps the full union.
2. **Pinyin is dict-sourced.** v1 derived pinyin from Hanzi via
  `pypinyin`, which always picks the most common reading per character
   and gets homonyms wrong about as often as you'd expect (~1% of rows).
   v2 looks pinyin up from the same dictionary Migaku itself ships in
   the browser extension, so 行 with `Sense # = 1` now reads `háng`
   instead of `xíng`. `pypinyin` is retained as a fallback for words
   the dict doesn't contain.
3. **Meaning auto-populated on first v2 sync (rows that are currently
  blank only).** v1 always left `Meaning` blank for the user (or
   Notion AI) to fill. v2 writes Migaku's dict gloss into rows where
   it's blank — *only* on the first v2 sync against a given state.db.
   Once that one-time pass is done, the v1 rule kicks back in: Meaning
   is never overwritten. Existing v1 users with rows that already have
   meanings (AI-generated or hand-typed) are completely untouched.
   **Opt-out**: pass `--no-dict-meanings` on the first sync. v2 will
   then behave exactly like v1 (Meaning never written, blank or
   otherwise).
4. `**Frequency` column (new).** A 1-5 star rating matching what
  Migaku's UI shows. 5 = most common. Pulled from the dict's own
   field where available; otherwise computed from the per-language
   frequency database via quintile bucketing.
5. `**Example` column (new).** The first example sentence from the
  dict, where present. Notion-flavoured rich text.

For the **write sync** (`/push/enqueue` and media upload): both are new
in v2. Nothing to migrate.
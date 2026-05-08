# v1 ↔ v2 fail-rate math validation

## Why this doc exists

v1 read `fail_rate` / `total_reviews` / `failed_reviews` straight from
migoku's `/api/v1/words/difficult` endpoint, which was a thin wrapper
over a SQL aggregation against Migaku's local SRS SQLite (see
[`repository.go::GetDifficultWords`](../../migaku-notion/migoku/repository.go)).

The new Migaku HTTP API does **not** expose a difficulty endpoint. v2
gets the same numbers by aggregating in Python over the
`cards` + `cardWordRelations` + `reviews` arrays returned by
`/pull-sync` — see
[`migaku_notion.migaku.pull.compute_difficulty()`](../migaku_notion/migaku/pull.py).

The two paths *should* produce identical fail-rate / total-reviews /
failed-reviews per word (modulo float rounding). This document is the
methodology for proving that **once v2 is wired** — not a script to
run today.

## Why we relaxed the key

migoku's SQL grouped by `(dictForm, secondary, partOfSpeech)`. v2
deliberately groups by `(dictForm, secondary)` only.

Reason: Greg confirmed on **2026-05-07** that Migaku stores
`partOfSpeech` as a *list* on each word — observed across all 1500
words of his Mandarin course. Polysemous words like 行 carry both
`"v"` and `"n"` in their POS list. Keying on `partOfSpeech` would
splinter such a word into multiple aggregation buckets, which is the
opposite of what we want in Notion (one row per word, one fail rate,
one combined POS).

Concrete consequence for this validation:

  - **Same `fail_rate` / `total_reviews` / `failed_reviews`** are
    expected at the `(dictForm, secondary)` level. If v1 happened to
    return multiple rows for the same `(dictForm, secondary)` pair
    (one per POS), they need to be summed before comparing against
    v2's single row — see step 4 below for how the comparison
    handles this.
  - **`parts_of_speech` shape diverges by design.** v1 returns
    `partOfSpeech: <single string>` (whichever one won the SQL
    JOIN — usually a comma-joined string from SQLite text storage,
    e.g. `"v,n"`). v2 returns `parts_of_speech: list[str]` (the
    sorted, deduped union of POS values seen across every underlying
    row for that key). The pass criterion is set-membership: v2's
    list must be a *superset* of whatever v1 reported.

## Methodology

### 1. Capture a v1 baseline

With v1 still running:

```powershell
cd <v1>\sync
.\.venv\Scripts\Activate.ps1

# Make sure migoku has just synced fresh data — kill any open .venv, restart
# the docker container, wait ~10s for it to log "DB ready":
docker compose -f ..\migoku\docker-compose.yml restart

# Then dump the difficulty endpoint to JSON. Use the same lang and
# limit you'll use in v2.
$key = (Get-Content .env | Select-String '^MIGOKU_API_KEY=').ToString().Split('=')[1]
curl.exe -s "http://localhost:8080/api/v1/words/difficult?lang=zh&limit=2000" `
  -H "X-Api-Key: $key" `
  -o "..\..\migaku-notion-v2\tests\v1-difficult.json"
```

This file is the **authoritative oracle** for v2 to match.

### 2. Capture a v2 input snapshot

Immediately after step 1 (within the same minute, before any reviews
happen), pull `/pull-sync` once via v2 with the same auth token:

```powershell
cd <v2>
.\.venv\Scripts\Activate.ps1

# When pull_sync is wired, this would be the right invocation:
python -m migaku_notion _devtools dump-pull-sync `
  --output tests\v2-pull-sync.json `
  --server-version 0
```

(`_devtools` is a planned hidden subcommand for ad-hoc dumps; until it
exists, just call `pull.pull_sync()` from a one-off script and json-dump
the response.)

### 3. Run `compute_difficulty()` against the v2 snapshot

```python
import json
from migaku_notion.migaku.pull import compute_difficulty

payload = json.loads(open("tests/v2-pull-sync.json", encoding="utf-8").read())
v2 = compute_difficulty(payload, language="zh", limit=2000)
json.dump(v2, open("tests/v2-difficult.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
```

### 4. Cross-check

Compare `v1-difficult.json` against `v2-difficult.json`. Note the two
sides key DIFFERENTLY: v1 may emit multiple rows for the same
`(dictForm, secondary)` pair (one per partOfSpeech), v2 emits one. We
collapse v1 onto v2's key by summing total/failed reviews for any
multi-row groups before comparing — that's the only fair test given
the deliberate v2 key change (see "Why we relaxed the key" above).

```python
import json
from collections import defaultdict

v1 = json.load(open("tests/v1-difficult.json", encoding="utf-8"))
v2 = json.load(open("tests/v2-difficult.json", encoding="utf-8"))

def key(r): return (r["dictForm"], r["secondary"])

# Collapse v1 onto v2's relaxed key. If v1 emitted multiple POS rows
# for the same (dictForm, secondary), sum the review counts and
# recompute the fail_rate. Collect the union of POS strings as a set.
v1_collapsed: dict[tuple, dict] = {}
v1_pos_seen: dict[tuple, set] = defaultdict(set)
for r in v1:
    k = key(r)
    pos_field = r.get("partOfSpeech") or ""
    # v1 sometimes comma-joined inside one string ("v,n"); split.
    for p in (s.strip() for s in pos_field.split(",") if s.strip()):
        v1_pos_seen[k].add(p)
    bucket = v1_collapsed.setdefault(k, {
        "dictForm":       r["dictForm"],
        "secondary":      r["secondary"],
        "total_reviews":  0,
        "failed_reviews": 0,
        "knownStatus":    r.get("knownStatus"),
    })
    bucket["total_reviews"]  += r.get("total_reviews")  or 0
    bucket["failed_reviews"] += r.get("failed_reviews") or 0
for k, b in v1_collapsed.items():
    b["fail_rate"] = round(
        b["failed_reviews"] / b["total_reviews"] * 100, 2
    ) if b["total_reviews"] else 0.0

v2_by = {key(r): r for r in v2}

# Signal set: ONLY entries with actual review history. Words with no
# reviews / fail_rate == 0 OR null aren't a meaningful signal — Greg's
# explicit instruction (2026-05-07).
def has_signal(r):
    return (r.get("total_reviews") or 0) > 0 and (r.get("fail_rate") or 0) > 0

v1_signal = {k: r for k, r in v1_collapsed.items() if has_signal(r)}
print(f"v1 entries with signal: {len(v1_signal)}/{len(v1_collapsed)} "
      f"(collapsed from {len(v1)} v1 rows)")

mismatches = []
for k, r1 in v1_signal.items():
    r2 = v2_by.get(k)
    if r2 is None:
        mismatches.append((k, "missing in v2"))
        continue
    if abs((r1["fail_rate"] or 0) - (r2["fail_rate"] or 0)) > 0.01:
        mismatches.append((k, f"fail_rate {r1['fail_rate']} vs {r2['fail_rate']}"))
    if r1["total_reviews"] != r2["total_reviews"]:
        mismatches.append((k, f"total_reviews {r1['total_reviews']} vs {r2['total_reviews']}"))
    if r1["failed_reviews"] != r2["failed_reviews"]:
        mismatches.append((k, f"failed_reviews {r1['failed_reviews']} vs {r2['failed_reviews']}"))
    # POS check: v2's list should be a SUPERSET of whatever v1 saw.
    v1_pos = v1_pos_seen.get(k, set())
    v2_pos = set(r2.get("parts_of_speech") or [])
    if v1_pos and not v1_pos.issubset(v2_pos):
        mismatches.append((k, f"parts_of_speech v1={sorted(v1_pos)} not subset of v2={sorted(v2_pos)}"))

if not mismatches:
    print("PASS — v2 matches v1 across the signal set.")
else:
    for m in mismatches[:50]:
        print("MISMATCH:", m)
    print(f"... total {len(mismatches)} mismatches")
```

**Pass criteria** (all must hold across the signal set, i.e. words
where `total_reviews > 0` and `fail_rate > 0`):

| Field              | Tolerance                                                      |
|--------------------|----------------------------------------------------------------|
| `fail_rate`        | ±0.01 (rounding)                                               |
| `total_reviews`    | Exact (after collapsing v1 multi-POS rows onto v2's key)       |
| `failed_reviews`   | Exact (after collapsing v1 multi-POS rows onto v2's key)       |
| `parts_of_speech`  | Set membership: v2's list ⊇ v1's POS values for the same key   |
| Row presence       | Every v1 signal `(dictForm, secondary)` appears in v2          |

**Explicitly excluded** from the comparison (per Greg, 2026-05-07):
- Words with `total_reviews == 0` or missing — no signal.
- Words with `fail_rate` of `0` or `null` — also no signal; can be
  noise from Migaku's bookkeeping.
- Words below the `min_reviews=5` threshold (migoku's `HAVING
  total_reviews >= 5`). Both v1 and v2 should drop these.

### 5. Iterate

If any mismatch shows up, the most likely causes:

- **Tombstone filter difference.** v1's WHERE clause excludes
  `w.del = 0 AND c.del = 0 AND r.del = 0`. v2 must check `del` on all
  three. The current algorithm outline does; if you change it, retain
  this.
- **Review type filter.** v1 includes `r.type IN (1, 2)`. Anything
  outside that pair (pre-card creation events, type 3+, etc.) must be
  ignored.
- **Rounding direction.** v1 uses SQL `ROUND(... * 100, 2)`. Python's
  `round()` uses banker's rounding by default, so a tie at `.5` may
  go differently. If a single-decimal mismatch shows up at exactly
  half a percent, this is the cause; switch to
  `Decimal(...).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`.
- **POS subset failure.** v2's `parts_of_speech` should be a superset
  of v1's. If it's missing values, the most likely cause is the
  `_normalise_pos()` helper not splitting v1-style comma-joined
  strings — verify by printing both sides for a known polysemous
  word like 行.

(Removed: "Card-relation join difference." — no longer relevant after
the 2026-05-07 key relaxation. v2 keys on `(dictForm, secondary)` by
design.)

### 6. Capture the result

Once it passes, drop a one-line note + the run date into
`tests/VALIDATION-LOG.md` so we don't re-run it on every change. If
either v1 or Migaku itself changes, re-run.

## Tear-down

```powershell
del tests\v1-difficult.json
del tests\v2-difficult.json
del tests\v2-pull-sync.json
```

(All three are in `.gitignore` via the existing `*.json` /  `*.har`
patterns? Double-check before committing if you decide to keep them.
The validation snapshots contain Migaku review data which is mildly
sensitive; do not commit.)


# Pinyin + Meaning validation (informational)

> **No automated pass/fail criterion.** v2 is *intended* to differ from
> v1 on a small subset of words because the new pinyin source
> (Migaku's published dict) is more accurate than v1's `pypinyin`
> derivation. This section documents the spot-check methodology, not
> a regression gate.

## Methodology

### 1. Sample

Pick ~50 Mandarin words from your existing v1-populated state.db.
Bias the sample toward likely homonyms — the v2 wins live there:

```python
import sqlite3
conn = sqlite3.connect("state.db")
conn.row_factory = sqlite3.Row
sample = list(conn.execute("""
    SELECT dict_form, secondary, pinyin_marks AS v1_pinyin
    FROM words
    WHERE lang = 'zh' AND archived = 0
      AND (
           dict_form LIKE '%行%'
        OR dict_form LIKE '%为%'
        OR dict_form LIKE '%重%'
        OR dict_form LIKE '%乐%'
        OR dict_form LIKE '%长%'
        OR secondary != '0'              -- any non-default sense
      )
    LIMIT 50
"""))
```

(Add more polysemous-Hanzi filters as you spot them.)

### 2. v2 lookup

Once `MigakuDict.lookup` is wired, look each one up:

```python
from migaku_notion.migaku.dict import MigakuDict
md = MigakuDict("zh_CN", target_lang="en")
for s in sample:
    entry = md.lookup(s["dict_form"], sense_index=s["secondary"])
    print(f"{s['dict_form']!s:>6} #{s['secondary']!s:>2}  v1={s['v1_pinyin']!r:<20}  "
          f"v2={(entry.reading if entry else '(miss)')!r}")
```

### 3. Manual review

Walk the printout. Three cases:

| Case | What it means | Action |
|---|---|---|
| **Identical** | Most rows. v1 and v2 both correct. | None — expected baseline. |
| **Different, v2 looks right** | Homonym disambiguation win, e.g. 行 sense=1 going from `xíng`→`háng`. | None — confirms v2's improvement. Spot-check ≥3 of these against Migaku's own UI to be sure. |
| **Different, v2 looks wrong** | Either the dict has a stale entry, the `Sense #` mapping in your state.db doesn't match what Migaku's dict expects, or `_normalise_pos()` mishandled a list value. | File an issue + add the case to a "regression rows" subsection here so future runs catch it. |
| **`(miss)`** | Word isn't in the dict. v2 falls back to `pypinyin` and produces v1's old value. | Expected for proper nouns / made-up vocab. Should be rare for course content. If common, the dict catalog code is probably picking the wrong language file. |

### 4. Meaning spot-check

Same sample, look at `entry.meaning`. Compare against:
- The user's existing Notion `Meaning` column for that row (if any).
- Migaku's own UI definition for that word.

Expectation: matches Migaku's UI (same source). If your Notion AI
previously generated something more bespoke, that's fine — the
first-v2-sync auto-populate path skips non-blank Meanings (see
`MIGRATION-FROM-V1.md`).

### 5. Capture

Drop a one-line note + the run date into `tests/VALIDATION-LOG.md`
alongside the fail-rate result. No formal pass/fail.

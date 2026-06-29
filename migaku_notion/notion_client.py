"""Notion REST wrapper sized for our upsert workflow.

Ported verbatim from v1's `sync.py::NotionClient`. Intentionally minimal:
no SDK, raw HTTP, ~100 LoC. The throttle (~2.5 rps) is well below Notion's
3 rps limit; the retry loop covers transient ConnectionError /
ChunkedEncodingError / 429 / 5xx.

Also exposes the small bag of helpers v1 used to project Notion property
payloads back into `CachedRow`s when bootstrapping the cache.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from .models import CachedRow


NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"


class NotionClient:
    """Tiny Notion API wrapper sized for our upsert workflow."""

    REQUEST_INTERVAL = 0.4   # ~2.5 rps; below Notion's 3 rps cap.

    def __init__(self, token: str, database_id: str) -> None:
        self.token = token
        self.database_id = database_id
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = self.REQUEST_INTERVAL - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        self._throttle()
        url = f"{NOTION_API}{path}"
        for attempt in range(5):
            try:
                resp = self.session.request(method, url, timeout=60, **kw)
            except (requests.exceptions.ReadTimeout,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError):
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", "1")))
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if not resp.ok:
                raise RuntimeError(f"Notion {method} {path} -> {resp.status_code}: {resp.text[:500]}")
            return resp.json()
        raise RuntimeError(f"Notion {method} {path} failed after 5 attempts")

    def query_all_pages(self) -> list[dict[str, Any]]:
        """Fetch every page in the database. Returns the raw page objects."""
        pages: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = self._request("POST", f"/databases/{self.database_id}/query", json=body)
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return pages

    def create_page(self, properties: dict[str, Any]) -> dict[str, Any]:
        body = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
        }
        return self._request("POST", "/pages", json=body)

    def update_page(self, page_id: str, properties: dict[str, Any],
                    archived: bool | None = None) -> dict[str, Any]:
        """PATCH a page's properties. If `archived` is given, also (un)archives it."""
        body: dict[str, Any] = {"properties": properties}
        if archived is not None:
            body["archived"] = archived
        return self._request("PATCH", f"/pages/{page_id}", json=body)

    def archive_page(self, page_id: str) -> None:
        self._request("PATCH", f"/pages/{page_id}", json={"archived": True})

    def get_database(self) -> dict[str, Any]:
        """Fetch the database object — used to inspect the current schema."""
        return self._request("GET", f"/databases/{self.database_id}")

    def get_database_parent_page_id(self) -> str | None:
        """Return the parent page id of the configured database, if any."""
        parent = self.get_database().get("parent") or {}
        if parent.get("type") == "page_id":
            return parent.get("page_id")
        return None

    def update_database_properties(self, properties: dict[str, Any]) -> dict[str, Any]:
        """PATCH the database's properties. Additive when the keys are new;
        also lets you tweak / rename existing ones."""
        return self._request(
            "PATCH", f"/databases/{self.database_id}",
            json={"properties": properties},
        )

    def update_database_description(self, rich_text: list[dict[str, Any]]) -> dict[str, Any]:
        """PATCH the database description shown at the top of the database page."""
        return self._request(
            "PATCH", f"/databases/{self.database_id}",
            json={"description": rich_text},
        )

    def list_block_children(self, block_id: str) -> list[dict[str, Any]]:
        """Fetch all direct child blocks for a page/block."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            path = f"/blocks/{block_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            data = self._request("GET", path)
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return out

    def append_block_children(
        self,
        block_id: str,
        children: list[dict[str, Any]],
        *,
        after: str | None = None,
    ) -> list[dict[str, Any]]:
        """Append child blocks, optionally after a specific existing block."""
        body: dict[str, Any] = {"children": children}
        if after:
            body["after"] = after
        data = self._request("PATCH", f"/blocks/{block_id}/children", json=body)
        return data.get("results", [])

    def update_block_text(self, block_id: str, block_type: str, text: str) -> dict[str, Any]:
        """Replace the rich-text contents of a simple text block."""
        return self._request(
            "PATCH",
            f"/blocks/{block_id}",
            json={block_type: {"rich_text": _rich(text)}},
        )

    def delete_block(self, block_id: str) -> dict[str, Any]:
        """Delete a block from Notion."""
        return self._request("DELETE", f"/blocks/{block_id}")

    def find_sync_totals_section(self) -> dict[str, str] | None:
        """Find the old parent-page sync totals section, if present."""
        parent_page_id = self.get_database_parent_page_id()
        if not parent_page_id:
            return None

        children = self.list_block_children(parent_page_id)
        for i, block in enumerate(children):
            if block.get("type") != "heading_2":
                continue
            if _block_text(block) != "Sync totals":
                continue
            tail = children[i:i + 6]
            if len(tail) < 6:
                break
            expected = [
                "heading_2",
                "heading_3",
                "paragraph",
                "heading_3",
                "bulleted_list_item",
                "bulleted_list_item",
            ]
            if [b.get("type") for b in tail] != expected:
                break
            return {
                "section_heading": tail[0]["id"],
                "total_heading": tail[1]["id"],
                "total_value": tail[2]["id"],
                "status_heading": tail[3]["id"],
                "known_value": tail[4]["id"],
                "learning_value": tail[5]["id"],
            }
        return None

    def remove_sync_totals_section(self) -> None:
        """Remove the legacy parent-page sync totals section if it exists."""
        ids = self.find_sync_totals_section()
        if not ids:
            return
        for block_id in reversed(list(ids.values())):
            self.delete_block(block_id)

    def ensure_sync_totals_section(self) -> dict[str, str] | None:
        """Ensure the parent page has a live-updated sync totals section.

        Placement note: the public Notion API cannot prepend blocks ahead of an
        existing child database, so we insert the section immediately *after*
        the Migaku Vocab database block.
        """
        parent_page_id = self.get_database_parent_page_id()
        if not parent_page_id:
            return None

        children = self.list_block_children(parent_page_id)
        found = self.find_sync_totals_section()
        if found is not None:
            return found

        after_id = None
        for block in children:
            if block.get("type") == "child_database":
                after_id = block.get("id")
                break

        new_blocks = [
            _simple_text_block("heading_2", "Sync totals"),
            _simple_text_block("heading_3", "Total words"),
            _simple_text_block("paragraph", "0"),
            _simple_text_block("heading_3", "By status"),
            _simple_text_block("bulleted_list_item", "Total known words: 0"),
            _simple_text_block("bulleted_list_item", "Total learning words: 0"),
        ]
        created = self.append_block_children(parent_page_id, new_blocks, after=after_id)
        if len(created) != 6:
            return None
        return {
            "section_heading": created[0]["id"],
            "total_heading": created[1]["id"],
            "total_value": created[2]["id"],
            "status_heading": created[3]["id"],
            "known_value": created[4]["id"],
            "learning_value": created[5]["id"],
        }

    def update_sync_totals(
        self,
        section_ids: dict[str, str],
        *,
        total_words: int,
        total_known: int,
        total_learning: int,
    ) -> None:
        """Update the running total counters in the parent page section."""
        self.update_block_text(section_ids["total_value"], "paragraph", str(total_words))
        self.update_block_text(
            section_ids["known_value"],
            "bulleted_list_item",
            f"Total known words: {total_known}",
        )
        self.update_block_text(
            section_ids["learning_value"],
            "bulleted_list_item",
            f"Total learning words: {total_learning}",
        )


# ---------------------------------------------------------------------------
# Property extraction helpers (pure, no I/O)
# ---------------------------------------------------------------------------

def prop_text(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    if "title" in prop:
        return "".join(t.get("plain_text", "") for t in (prop.get("title") or []))
    if "rich_text" in prop:
        return "".join(t.get("plain_text", "") for t in (prop.get("rich_text") or []))
    if "select" in prop:
        sel = prop.get("select") or {}
        return sel.get("name") or ""
    return ""


def _block_text(block: dict[str, Any]) -> str:
    """Extract plain text from a simple Notion block payload."""
    block_type = block.get("type")
    if not block_type:
        return ""
    payload = block.get(block_type) or {}
    rich = payload.get("rich_text") or []
    return "".join(t.get("plain_text", "") for t in rich)


def prop_number(prop: dict[str, Any] | None) -> float | None:
    if not prop:
        return None
    return prop.get("number")


def prop_date_start(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    d = prop.get("date") or {}
    return d.get("start")


def cache_row_from_notion_page(page: dict[str, Any]) -> CachedRow | None:
    """Project a Notion page object into a CachedRow.

    Returns None if the page has no Migaku key (those rows are out of scope —
    e.g. a user-added row in the Vocab DB that doesn't correspond to a
    Migaku word).

    Also captures whether the Notion-side Meaning column was empty at
    bootstrap time, into `notion_meaning_was_blank`. This is the gate
    for the v2 first-sync auto-populate of dict-derived meanings: only
    rows where the user hasn't already filled Meaning (manually or via
    Notion AI) are eligible.
    """
    props = page.get("properties", {}) or {}
    key = prop_text(props.get("Migaku key"))
    if not key:
        return None
    parts = key.split("|", 2)
    if len(parts) != 3:
        return None
    lang, dict_form, secondary = parts
    total = prop_number(props.get("Total reviews"))
    failed = prop_number(props.get("Failed reviews"))
    freq = prop_number(props.get("Frequency"))
    meaning = prop_text(props.get("Meaning"))
    example = prop_text(props.get("Example"))
    return CachedRow(
        migaku_key=key,
        page_id=page["id"],
        lang=lang,
        dict_form=dict_form,
        secondary=secondary,
        known_status=prop_text(props.get("Status")) or None,
        fail_rate=prop_number(props.get("Fail rate %")),
        total_reviews=int(total) if total is not None else None,
        failed_reviews=int(failed) if failed is not None else None,
        part_of_speech=prop_text(props.get("Part of speech")) or None,
        last_synced=prop_date_start(props.get("Last synced")),
        archived=bool(page.get("archived", False)),
        pinyin_marks=prop_text(props.get("Pinyin")) or None,
        pinyin_numeric=prop_text(props.get("Pinyin (numeric)")) or None,
        sense_index=prop_text(props.get("Sense #")) or None,
        meaning=meaning or None,
        example=example or None,
        frequency_stars=int(freq) if freq is not None else None,
        notion_meaning_was_blank=not meaning,
    )


# ---------------------------------------------------------------------------
# Property builders (write side)
# ---------------------------------------------------------------------------

def _rich(text: str | None) -> list[dict[str, Any]]:
    if not text:
        return []
    return [{"type": "text", "text": {"content": text[:1900]}}]


def _rich_bold_multiline(lines: list[str]) -> list[dict[str, Any]]:
    """Build a bold Notion rich_text array with explicit line breaks."""
    out: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        content = line if i == len(lines) - 1 else line + "\n"
        out.append(
            {
                "type": "text",
                "text": {"content": content[:1900]},
                "annotations": {
                    "bold": True,
                    "italic": False,
                    "strikethrough": False,
                    "underline": False,
                    "code": False,
                    "color": "default",
                },
            }
        )
    return out


def build_database_totals_description(
    *,
    total_words: int,
    total_known: int,
    total_learning: int,
    unique_known_chars: int,
    unique_known_learning_chars: int,
) -> list[dict[str, Any]]:
    """Top-of-page totals block for the database description."""
    return _rich_bold_multiline(
        [
            f"Total words: {total_words}",
            f"Total known words: {total_known}",
            f"Total learning words: {total_learning}",
            f"Unique known characters: {unique_known_chars}",
            f"Unique known + learning characters: {unique_known_learning_chars}",
        ]
    )


def _simple_text_block(block_type: str, text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": _rich(text)},
    }


def format_parts_of_speech(value: Any) -> str:
    """Normalise a partOfSpeech value into the comma-separated string the
    Notion `Part of speech` column expects.

    Handles three input shapes:
      - None / "" -> ""
      - "v" or "v,n"          (legacy single-string form, kept for v1 compat)
      - ["v", "n"]            (modern Migaku shape; confirmed 2026-05-07)

    Output: "v" or "v, n" (sorted, deduped, space after comma so it reads
    nicely in the Notion UI).
    """
    if value is None or value == "":
        return ""
    if isinstance(value, (list, tuple, set)):
        items = [str(p).strip() for p in value if p and str(p).strip()]
    else:
        items = [p.strip() for p in str(value).split(",") if p.strip()]
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    deduped.sort()
    return ", ".join(deduped)


def build_properties(word: Any, *, include_meaning: bool, now_iso: str) -> dict[str, Any]:
    """Build the Notion `properties` payload for a Word.

    Same behaviour as v1: `include_meaning=True` only on new pages (sets
    Meaning to blank); updates leave Meaning untouched so any AI-generated
    meaning the user wrote in Notion survives.

    For zh: Pinyin = tone marks, Pinyin (numeric) = numeric tones,
    Sense # = migaku's `secondary` index.
    For non-zh: Pinyin = `secondary` (kana for ja, etc.); other two blank.
    """
    if word.language == "zh":
        pinyin_main = word.pinyin_marks or ""
        pinyin_numeric = word.pinyin_numeric or ""
        sense = word.secondary or ""
    else:
        pinyin_main = word.secondary or ""
        pinyin_numeric = ""
        sense = ""

    props: dict[str, Any] = {
        "Word":             {"title": _rich(word.dict_form)},
        "Pinyin":           {"rich_text": _rich(pinyin_main)},
        "Pinyin (numeric)": {"rich_text": _rich(pinyin_numeric)},
        "Sense #":          {"rich_text": _rich(sense)},
        "Status":           {"select": {"name": word.known_status} if word.known_status else None},
        "Language":         {"select": {"name": word.language}},
        "Last synced":      {"date": {"start": now_iso}},
        "Migaku key":       {"rich_text": _rich(word.key)},
    }
    if word.fail_rate is not None:
        props["Fail rate %"] = {"number": round(word.fail_rate, 2)}
    if word.total_reviews is not None:
        props["Total reviews"] = {"number": word.total_reviews}
    if word.failed_reviews is not None:
        props["Failed reviews"] = {"number": word.failed_reviews}
    # `word.part_of_speech` is typed `str | None` on the Word dataclass,
    # but in practice the eventual difficulty-merge step may have folded
    # a list[str] of POS values into it (Migaku stores POS as a list per
    # word — confirmed 2026-05-07). format_parts_of_speech() handles
    # all three shapes and returns "v, n" / "v" / "" deterministically.
    pos_text = format_parts_of_speech(word.part_of_speech)
    if pos_text:
        props["Part of speech"] = {"rich_text": _rich(pos_text)}

    # v2 additions: dictionary-derived enrichment.
    freq = getattr(word, "frequency_stars", None)
    if freq is not None:
        props["Frequency"] = {"number": int(freq)}
    example = getattr(word, "example", None)
    if example:
        props["Example"] = {"rich_text": _rich(example)}

    # Meaning policy. v1 rule (and the v2 default after the first sync):
    # never include Meaning in update payloads. v2 first-sync exception:
    # caller passes `include_meaning=True` AND `word.meaning` is non-blank
    # to write the dict-derived meaning into a row that's currently empty
    # in Notion. The "currently empty" check lives in the sync flow
    # (using `cached.notion_meaning_was_blank`); we just write what the
    # caller asks for here.
    if include_meaning:
        meaning_text = getattr(word, "meaning", None) or ""
        props["Meaning"] = {"rich_text": _rich(meaning_text) if meaning_text else []}
    return props


# ---------------------------------------------------------------------------
# Database creation (used by setup wizard)
# ---------------------------------------------------------------------------

NOTION_DB_TITLE_DEFAULT = "Migaku Vocab"
NOTION_DB_DESCRIPTION = (
    "Words synced from Migaku via the migaku-notion v2 tool. The Meaning "
    "column is meant to be filled in by you / Notion AI; do not edit other "
    "columns as they will be overwritten on each sync."
)

NOTION_DB_PROPERTIES: dict[str, Any] = {
    "Word":             {"title": {}},
    "Pinyin":           {"rich_text": {}},
    "Meaning":          {"rich_text": {}},
    "Example":          {"rich_text": {}},          # v2 (paired with Meaning)
    "Pinyin (numeric)": {"rich_text": {}},
    "Status": {
        "select": {
            "options": [
                {"name": "KNOWN",    "color": "green"},
                {"name": "LEARNING", "color": "yellow"},
                {"name": "UNKNOWN",  "color": "gray"},
                {"name": "TRACKED",  "color": "blue"},
                {"name": "IGNORED",  "color": "red"},
            ]
        }
    },
    "Frequency":        {"number": {"format": "number"}},   # v2 (1-5 stars)
    "Fail rate %":      {"number": {"format": "number"}},
    "Total reviews":    {"number": {"format": "number"}},
    "Failed reviews":   {"number": {"format": "number"}},
    "Part of speech":   {"rich_text": {}},
    "Language": {
        "select": {
            "options": [
                {"name": "zh", "color": "orange"},
                {"name": "ja", "color": "blue"},
                {"name": "en", "color": "purple"},
                {"name": "es", "color": "yellow"},
            ]
        }
    },
    "Last synced":      {"date": {}},
    "Migaku key":       {"rich_text": {}},
    "Sense #":          {"rich_text": {}},
}


# Subset of the schema added in v2. Used by the setup wizard to ALTER
# an existing v1 database in-place — `databases.update` is additive,
# so PATCHing with these keys appends them without disturbing v1's
# existing columns. Order here is deliberate but Notion doesn't honour
# it on update (column position is owned by the saved view); v2
# handles ordering at view-edit time, not schema-edit time.
NOTION_V2_NEW_PROPERTIES: dict[str, Any] = {
    "Frequency": NOTION_DB_PROPERTIES["Frequency"],
    "Example":   NOTION_DB_PROPERTIES["Example"],
}


def create_database(token: str, parent_page_id: str, *,
                    title: str = NOTION_DB_TITLE_DEFAULT) -> tuple[str, str]:
    """Create the Migaku Vocab database under `parent_page_id`. Returns (id, url)."""
    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": title}}],
        "description": [{"type": "text", "text": {"content": NOTION_DB_DESCRIPTION}}],
        "properties": NOTION_DB_PROPERTIES,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    resp = requests.post(f"{NOTION_API}/databases", headers=headers, json=body, timeout=30)
    if resp.status_code == 401:
        raise RuntimeError("Notion rejected the token (401). Double-check NOTION_TOKEN.")
    if resp.status_code == 404:
        raise RuntimeError(
            "Notion couldn't find the parent page (404). The integration probably isn't "
            "connected to it yet — open the page in Notion -> ... -> Connections -> "
            "Connect to -> pick your integration, then re-run setup."
        )
    if not resp.ok:
        raise RuntimeError(f"Notion POST /databases -> {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    return data["id"], data.get("url") or ""


def upgrade_database_to_v2(notion: NotionClient) -> dict[str, str]:
    """Add the v2 columns (`Frequency`, `Example`) to an existing v1
    Migaku Vocab database, leaving everything else untouched.

    Idempotent: inspects `databases.<id>` first and only PATCHes with
    the columns that aren't already there. Returns a dict reporting
    what was added vs. skipped, e.g. `{"Frequency": "added", "Example":
    "skipped"}`.

    Use during `setup` (existing v1 users) and skip on fresh `setup`
    runs that just created the database with the full schema. Safe to
    re-run.
    """
    info = notion.get_database()
    existing_props = (info.get("properties") or {}).keys()

    to_add: dict[str, Any] = {}
    report: dict[str, str] = {}
    for name, schema in NOTION_V2_NEW_PROPERTIES.items():
        if name in existing_props:
            report[name] = "skipped"
        else:
            to_add[name] = schema
            report[name] = "added"

    if to_add:
        notion.update_database_properties(to_add)
    return report

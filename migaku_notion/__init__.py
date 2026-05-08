"""migaku-notion v2 — direct Migaku API → Notion sync (no Docker, no Go).

Package layout:
    migaku_notion.cli           CLI argparse dispatch (mirrors v1's `sync.py`)
    migaku_notion.config        env loading, paths, defaults
    migaku_notion.models        Word / CachedRow / MigakuEntity dataclasses
    migaku_notion.state         StateCache (SQLite, identical schema to v1)
    migaku_notion.notion_client NotionClient (HTTP, throttled, retrying)
    migaku_notion.pinyin        pypinyin wrappers (tone marks + numeric)
    migaku_notion.export        CSV / XLSX writers
    migaku_notion.migaku.*      Direct talk to core-server / file-sync / auth
    migaku_notion.commands.*    One module per CLI subcommand

The Migaku API is captured live in
    <preply-migaku>/lesson_samples/migaku-card-creator.har
and inspected with `inspect_har.py` (same script, copied into this repo for
convenience). Every stub in `migaku_notion.migaku.*` references that HAR.
"""

__version__ = "0.1.0a1"

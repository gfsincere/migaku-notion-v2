"""`migaku-notion setup` — interactive first-run wizard.

End-to-end Firebase-based wizard. Mirrors the structure of v1's
`run_setup` but:
  - logs into Migaku directly via Firebase email-password (no migoku);
  - mints (and persists) a stable MIGAKU_DEVICE_ID;
  - upgrades existing v1 Notion DBs in-place (adds Frequency / Example);
  - downloads the Migaku dictionary + frequency .db files into
    `~/.migaku-notion-v2/dicts/<lang>/`.
"""
from __future__ import annotations

import argparse
import getpass
import logging
import re
import webbrowser
from typing import Any

from .. import config
from ..migaku import auth
from ..migaku.dict import MigakuDict
from ..migaku.frequency import MigakuFrequency
from ..notion_client import (
    NotionClient,
    create_database,
    upgrade_database_to_v2,
)


log = logging.getLogger("migaku-notion")


# ---------------------------------------------------------------------------
# Prompt helpers (port of v1's _prompt + _extract_notion_page_id)
# ---------------------------------------------------------------------------

def _prompt(label: str, *, current: str | None = None, secret: bool = False,
            allow_blank: bool = False, default: str | None = None) -> str:
    while True:
        suffix = ""
        if current:
            suffix = " (press enter to keep current value)"
        elif default:
            suffix = f" [{default}]"
        if secret:
            value = getpass.getpass(f"  {label}{suffix}: ").strip()
        else:
            value = input(f"  {label}{suffix}: ").strip()
        if not value:
            if current:
                return current
            if default:
                return default
            if allow_blank:
                return ""
            print("    (required — please enter a value)")
            continue
        return value


def _extract_notion_page_id(raw: str) -> str | None:
    raw = raw.strip()
    m = re.search(r"([0-9a-fA-F]{32})", raw.replace("-", ""))
    if m:
        return m.group(1).lower()
    return None


def _yes_no(label: str, *, default_yes: bool = True) -> bool:
    prompt = " [Y/n]: " if default_yes else " [y/N]: "
    while True:
        try:
            value = input(label + prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if not value:
            return default_yes
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("    (please answer y or n)")


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:  # noqa: C901  (linear wizard)
    print()
    print("==============================================================")
    print("  Migaku-Notion v2 setup wizard")
    print("==============================================================")
    print()
    print("This will walk you through the one-time configuration:")
    print("  1. Migaku login (email/password -> Firebase refresh token)")
    print("  2. Optional Notion integration token + parent page")
    print("  3. (If enabled) auto-create the Migaku Vocab database, OR")
    print("     upgrade an existing v1 database with the new v2 columns")
    print("  4. Mint a stable MIGAKU_DEVICE_ID for this install")
    print("  5. Download Migaku's published Mandarin dictionary +")
    print("     frequency database (~30 MB total)")
    print()
    print("Existing values in .env are kept unless you pass --force.")
    print()

    existing = config._read_env_file()
    if args.force:
        existing = {}

    # --- Section 1: Migaku creds + Firebase login ------------------------
    print("--- 1. Migaku login (Firebase) ---")
    print("  Your normal Migaku account credentials. v2 trades them for")
    print("  a long-lived refresh token; the password is not stored.")
    email = _prompt("Migaku email", current=existing.get("MIGAKU_EMAIL"))
    refresh_token = existing.get("MIGAKU_REFRESH_TOKEN") or ""
    password = ""
    if not refresh_token or args.force:
        password = _prompt("Migaku password", secret=True)
    print()

    print("  Authenticating with Migaku ...")
    try:
        if password:
            session = auth.AuthSession.from_email_password(email, password)
        else:
            session = auth.AuthSession.from_refresh_token(refresh_token)
        refresh_token = session.refresh_token
        print(f"  Login OK. Refresh token persisted to .env.")
    except RuntimeError as exc:
        print(f"  ERROR: {exc}")
        return 1
    print()

    # --- Section 2/3: Optional Notion integration ------------------------
    notion_enabled = _yes_no("Enable Notion integration now?", default_yes=True)
    notion_token = existing.get("NOTION_TOKEN", "")
    db_id = existing.get("NOTION_DATABASE_ID", "")
    db_url = ""

    if notion_enabled:
        print("--- 2. Notion integration ---")
        if not notion_token or args.force:
            print("  You need a Notion 'internal integration' so this script can")
            print("  read/write your database. I'll open the integrations page in")
            print("  your browser. Create one (any name, default capabilities),")
            print("  copy the 'Internal Integration Secret' and paste it here.")
            try:
                input("  Press enter to open https://www.notion.so/profile/integrations ... ")
                webbrowser.open("https://www.notion.so/profile/integrations")
            except KeyboardInterrupt:
                return 130
        notion_token = _prompt(
            "Notion integration secret",
            current=existing.get("NOTION_TOKEN"), secret=True,
        )
        print()

        if db_id and not args.force:
            print(f"--- 3. Notion database (already configured: {db_id}) ---")
            print("  Checking schema for v2 columns (Frequency, Example) ...")
            try:
                notion = NotionClient(notion_token, db_id)
                report = upgrade_database_to_v2(notion)
                for col, status in report.items():
                    marker = "+" if status == "added" else "."
                    print(f"    {marker} {col}: {status}")
            except RuntimeError as exc:
                print(f"  ERROR: {exc}")
                return 1
        else:
            print("--- 3. Notion parent page + database ---")
            print("  Decide where the Migaku Vocab database should live (e.g. a")
            print("  page called 'Mandarin' or 'Migaku Word List'). Then:")
            print("    a) Open that page in Notion")
            print("    b) Top-right ... -> Connections -> Connect to -> pick")
            print("       the integration you just created")
            print("    c) Copy the page URL (or page ID) and paste it here")
            parent_id = ""
            while not parent_id:
                raw = _prompt("Notion parent page URL or ID")
                parent_id = _extract_notion_page_id(raw)
                if not parent_id:
                    print("    (couldn't find a 32-hex page ID in that. try again.)")
            print()
            print("  Creating the Migaku Vocab database (full v2 schema) ...")
            try:
                db_id, db_url = create_database(notion_token, parent_id)
                print(f"    Created: {db_id}")
                if db_url:
                    print(f"    URL:     {db_url}")
            except RuntimeError as exc:
                print(f"  ERROR: {exc}")
                return 1
        print()
    else:
        print("--- 2/3. Notion integration ---")
        print("  Skipping Notion setup. v2 will run in local-only mode until")
        print("  you add NOTION_TOKEN and NOTION_DATABASE_ID later.")
        notion_token = ""
        db_id = ""
        print()

    # --- Section 4: Device id -----------------------------------------
    print("--- 4. Device identity ---")
    device_id = existing.get("MIGAKU_DEVICE_ID") or config.get_or_create_device_id()
    print(f"  MIGAKU_DEVICE_ID = {device_id[:8]}... (32-hex, persisted to .env)")
    print()

    # --- Section 5: Dictionary + frequency download -------------------
    lang_for_dict = existing.get("SYNC_LANG", "zh") or "zh"
    print(f"--- 5. Migaku dictionary + frequency DB (lang={lang_for_dict}) ---")
    try:
        md = MigakuDict(lang_for_dict)
        dict_path = md.ensure_downloaded()
        print(f"  Dict:      {dict_path}")
        mf = MigakuFrequency(lang_for_dict)
        freq_path = mf.ensure_downloaded()
        print(f"  Frequency: {freq_path}")
    except Exception as exc:    # noqa: BLE001  (catalog/network errors)
        print(f"  WARNING: dict download failed ({exc}). The sync will still")
        print("  run, but pinyin/meaning/example will fall back to pypinyin")
        print("  and the Frequency column will stay blank. Re-run `setup` to retry.")
    print()

    # --- Write .env ---------------------------------------------------
    print("--- 6. Writing .env ---")
    new_env: dict[str, Any] = {
        **existing,
        "MIGAKU_EMAIL":         email,
        "MIGAKU_REFRESH_TOKEN": refresh_token,
        "MIGAKU_DEVICE_ID":     device_id,
    }
    if notion_enabled:
        new_env["NOTION_TOKEN"] = notion_token
        new_env["NOTION_DATABASE_ID"] = db_id
    else:
        new_env.pop("NOTION_TOKEN", None)
        new_env.pop("NOTION_DATABASE_ID", None)
    new_env.setdefault("SYNC_LANG", "zh")
    new_env.setdefault("SYNC_STATUS", "KNOWN,LEARNING")
    new_env.setdefault("SYNC_DIFFICULT_LIMIT", "2000")
    # Don't write MIGAKU_PASSWORD by default; the refresh token replaces it.
    new_env.pop("MIGAKU_PASSWORD", None)
    config._write_env_file(new_env)
    print(f"  Wrote {config.ENV_PATH}")
    print()

    print("==============================================================")
    print("  Setup complete.")
    print("==============================================================")
    print()
    print("Next:")
    print("  1. Verify connectivity:          python -m migaku_notion status")
    if notion_enabled:
        print("  2. Preview the sync:             python -m migaku_notion sync --dry-run")
        print("  3. Run the sync:                 python -m migaku_notion sync")
    else:
        print("  2. Preview local-only sync:      python -m migaku_notion sync --dry-run --no-notion")
        print("  3. Run local-only sync:          python -m migaku_notion sync --no-notion")
    print()
    return 0

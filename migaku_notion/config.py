"""Centralised env loading and well-known paths.

Importing this module:
  - calls `dotenv.load_dotenv()` exactly once (so any sibling import of
    `os.environ.get(...)` sees the same view)
  - exposes the canonical paths we use everywhere (state.db, .env, etc.)
  - exposes typed accessors for the common config knobs (lang, statuses,
    difficult-limit) with the same defaults as v1.
  - exposes `get_or_create_device_id()` — the one helper that may *write*
    to .env (everything else here is read-only).

This is intentionally tiny — there is no settings-object, no pydantic. Treat
env vars as the source of truth and read them at the call site if you need
something one-off.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()

log = logging.getLogger("migaku-notion")


PROJECT_ROOT = Path(__file__).resolve().parent.parent

ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"

STATE_DB_PATH = PROJECT_ROOT / "state.db"

# Where Migaku's published dictionary + frequency .db files get cached.
# Lives under the user's home so multiple v2 checkouts share one copy
# (the files are tens of MB and identical regardless of working
# directory). Override with $MIGAKU_NOTION_DICTS_DIR for testing.
DICTS_DIR = Path(
    os.getenv("MIGAKU_NOTION_DICTS_DIR")
    or (Path.home() / ".migaku-notion-v2" / "dicts")
)


DEFAULT_LANG = os.getenv("SYNC_LANG", "zh")
DEFAULT_STATUS = os.getenv("SYNC_STATUS", "KNOWN,LEARNING")
DEFAULT_DIFFICULT_LIMIT = int(os.getenv("SYNC_DIFFICULT_LIMIT", "2000"))


def notion_token() -> str | None:
    return os.getenv("NOTION_TOKEN")


def notion_database_id() -> str | None:
    return os.getenv("NOTION_DATABASE_ID")


def migaku_email() -> str | None:
    return os.getenv("MIGAKU_EMAIL")


def migaku_password() -> str | None:
    return os.getenv("MIGAKU_PASSWORD")


def migaku_id_token() -> str | None:
    """A previously-derived Firebase ID token (optional, short-lived).

    Mostly used for tests/scripts; the normal flow re-derives from the
    refresh token on each run via `migaku_notion.migaku.auth`.
    """
    return os.getenv("MIGAKU_ID_TOKEN")


def migaku_refresh_token() -> str | None:
    return os.getenv("MIGAKU_REFRESH_TOKEN")


def migaku_device_id() -> str | None:
    """Read-only accessor. Use `get_or_create_device_id()` if you also want
    one created and persisted on first run.
    """
    return os.getenv("MIGAKU_DEVICE_ID")


# ---------------------------------------------------------------------------
# .env upsert helper (used by get_or_create_device_id and the setup wizard)
# ---------------------------------------------------------------------------

def _read_env_file() -> dict[str, str]:
    """Parse the existing .env into a dict, preserving everything we don't
    touch. Returns {} if .env doesn't exist yet."""
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _write_env_file(values: dict[str, str]) -> None:
    """Write .env, preserving comments/order from .env.example as a template.

    Updated keys overwrite their corresponding lines; existing comments and
    structure stay intact. New keys not in the template are appended in a
    "Added by migaku-notion" block at the bottom.
    """
    if ENV_EXAMPLE_PATH.exists():
        lines = ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    seen: set[str] = set()
    out_lines: list[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, _ = line.partition("=")
            key = key.strip()
            if key in values:
                out_lines.append(f"{key}={values[key]}")
                seen.add(key)
                continue
        out_lines.append(line)

    extra = [k for k in values if k not in seen]
    if extra:
        out_lines.append("")
        out_lines.append("# Added by migaku-notion")
        for k in extra:
            out_lines.append(f"{k}={values[k]}")

    ENV_PATH.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def upsert_env_values(updates: dict[str, str]) -> None:
    """Merge `updates` into .env (creating it from .env.example if needed)
    and refresh the in-process os.environ so subsequent reads see the new
    values without a restart.
    """
    existing = _read_env_file()
    merged = {**existing, **updates}
    _write_env_file(merged)
    for k, v in updates.items():
        os.environ[k] = v


def get_or_create_device_id() -> str:
    """Return the persistent MIGAKU_DEVICE_ID, generating one on first run.

    v2 registers as a "device" with Migaku just like the browser extension
    or mobile app does. The deviceId is sent on every /pull-sync and
    /push/enqueue request (see HAR: `deviceId=2062608e0963eba76d10952e41560939`)
    so Migaku can route writes correctly and present a consistent view of
    our edits across runs.

    On first run we mint a fresh 32-hex string (matches the wire shape) and
    persist it back to .env so successive runs stay the same "device". This
    is also mirrored into state.db's `meta` table at sync time so we can
    sanity-check that .env and the cache agree.
    """
    existing = migaku_device_id()
    if existing:
        return existing

    fresh = secrets.token_hex(16)
    log.warning(
        "MIGAKU_DEVICE_ID was missing; generated a new one (%s...) and "
        "persisted it to .env. From now on this install identifies itself "
        "to Migaku as that 'device'. To reset, delete the line from .env.",
        fresh[:8],
    )
    upsert_env_values({"MIGAKU_DEVICE_ID": fresh})
    return fresh

"""`migaku-notion login` — derive a long-lived Firebase refresh token.

Run this once after first install (or after a password reset). It calls
`identitytoolkit.googleapis.com/v1/accounts:signInWithPassword`, writes
the resulting refresh token back into `.env` as `MIGAKU_REFRESH_TOKEN`,
and from then on `sync` / `status` re-derive a fresh id_token from the
refresh token via `securetoken.googleapis.com/v1/token` automatically.
"""
from __future__ import annotations

import argparse
import getpass
import logging

from .. import config
from ..migaku import auth


log = logging.getLogger("migaku-notion")


def run(args: argparse.Namespace) -> int:
    email = args.email or config.migaku_email()
    password = args.password or config.migaku_password()

    if not email:
        try:
            email = input("Migaku email: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 130
    if not password:
        try:
            password = getpass.getpass("Migaku password: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 130

    if not (email and password):
        log.error("Both email and password are required.")
        return 2

    try:
        token = auth.sign_in_with_password(email, password)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    updates = {
        "MIGAKU_EMAIL":         email,
        "MIGAKU_REFRESH_TOKEN": token.refresh_token,
    }
    # Don't persist password by default — once we have a refresh token,
    # email+password aren't needed for refreshes anymore. Keep whatever
    # was already in .env for MIGAKU_PASSWORD intact via upsert merge.
    config.upsert_env_values(updates)
    print()
    print("Migaku login successful.")
    print(f"  Account:                {email}")
    print(f"  Refresh token persisted to .env (MIGAKU_REFRESH_TOKEN).")
    print(f"  ID token expires at:   {token.expires_at:.0f} (epoch)")
    print()
    print("Subsequent runs of `sync` and `status` will refresh the id_token")
    print("automatically — you should not need to run `login` again unless")
    print("you change your Migaku password or rotate the refresh token.")
    return 0

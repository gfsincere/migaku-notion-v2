"""Entry point so `python -m migaku_notion <subcommand>` works."""
from __future__ import annotations

import sys

from migaku_notion.cli import main


if __name__ == "__main__":
    sys.exit(main())

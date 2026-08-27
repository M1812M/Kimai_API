#!/usr/bin/env python3
"""Simple Python entry point for the Kimai API project."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from kimai_import_export.config import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from kimai_import_export import clockify_backup, project_tasks  # noqa: E402


HELP = """\
Usage:
  python kimai_api.py import <csv-file-or-folder> [options]
  python kimai_api.py backup-clockify [options]
  python kimai_api.py backup-clockify --resume <backup-directory>
  python kimai_api.py verify-clockify-backup <backup-directory>

Examples:
  python kimai_api.py import .\\data\\ --offline
  python kimai_api.py import .\\data\\
  python kimai_api.py import .\\data\\ --non-billable --apply

The default import is a read-only Kimai preview. Data is created only when
--apply and either --billable or --non-billable are supplied.

Clockify backup commands are strictly read-only and store data under
.\\data\\clockify-backups by default.
"""


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help"}:
        print(HELP)
        return 0

    command = arguments.pop(0)
    if command == "import":
        return project_tasks.main(arguments)
    if command == "backup-clockify":
        return clockify_backup.backup_main(arguments)
    if command == "verify-clockify-backup":
        return clockify_backup.verify_main(arguments)

    print(f"ERROR: unknown command {command!r}.\n\n{HELP}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

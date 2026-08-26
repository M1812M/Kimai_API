"""Local environment loading without adding a runtime dependency."""

from __future__ import annotations

import os
import re
from pathlib import Path


_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_dotenv(path: Path | None = None) -> Path | None:
    """Load simple KEY=value pairs without overriding existing environment values.

    The default is ``.env`` in the current working directory. Blank lines,
    comments, and malformed lines are ignored. Single- or double-quoted values
    are unwrapped; values are otherwise preserved exactly after outer whitespace
    is removed.
    """

    environment_path = path or Path.cwd() / ".env"
    if not environment_path.is_file():
        return None

    for raw_line in environment_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not _ENVIRONMENT_KEY.fullmatch(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)

    return environment_path

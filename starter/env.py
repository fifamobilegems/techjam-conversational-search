"""Minimal local ``.env`` loader with no third-party dependency.

Environment variables set by the shell always win.  The loader exists so a
developer can enable optional local features without relying on an IDE plugin;
it never prints values, which is important for API keys.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_project_env(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.is_file():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)

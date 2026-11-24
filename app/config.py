"""Application configuration."""
from __future__ import annotations

import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _database_path() -> Path | str:
    raw = os.getenv("DB_PATH") or os.getenv("DATABASE_URL") or os.getenv("SQLITE_PATH") or os.getenv("DB_FILE")
    if not raw:
        return BASE_DIR / "data" / "ledger.db"
    if raw == ":memory:":
        return raw

    # Accept SQLAlchemy-style SQLite URLs as a convenience.
    match = re.match(r"^sqlite(?:\+[a-zA-Z0-9_]+)?://(?P<rest>.*)$", raw)
    if match:
        rest = match.group("rest")
        if rest in {"", ":memory:"}:
            return ":memory:"
        # //absolute/path -> /absolute/path; /relative/path is treated as relative.
        if rest.startswith("//"):
            rest = rest[1:]
        elif rest.startswith("/") and not raw.startswith("sqlite:////"):
            # url path form sqlite:///relative.db leaves "/relative.db" in some parsers.
            rest = rest.lstrip("/")
        raw = rest

    path = Path(raw).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


DB_PATH = _database_path()
SEED_DEMO_TREE = _bool("SEED_DEMO_TREE", True)
ALLOW_RESET = _bool("ALLOW_RESET", False)

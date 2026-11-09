from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def hermes_home() -> Path:
    """Return the active Hermes home, respecting HERMES_HOME."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def hub_db_path() -> Path:
    """Return the hub database path.

    HERMES_HUB_DB wins; otherwise use the project's data/ directory so the
    path does not depend on the current working directory when launched by a
    tunnel client.
    """
    raw = os.environ.get("HERMES_HUB_DB")
    if raw:
        return Path(raw).expanduser()
    return PROJECT_ROOT / "data" / "hub.db"


def state_db_path() -> Path:
    """Return Hermes' canonical session database path."""
    raw = os.environ.get("HERMES_STATE_DB")
    if raw:
        return Path(raw).expanduser()
    return hermes_home() / "state.db"


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

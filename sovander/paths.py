"""Where Sovander keeps its own files (Requirement 9.10: nothing outside these folders).

SOVANDER_HOME points every folder inside one directory, which tests use to stay hermetic.
"""

import os
from pathlib import Path

APP_DIR_NAME = "sovander"


def _base(xdg_var: str, fallback: str) -> Path:
    override = os.environ.get("SOVANDER_HOME")
    if override:
        return Path(override) / fallback.strip("./").replace("/", "-")
    return Path(os.environ.get(xdg_var) or Path.home() / fallback)


def data_dir() -> Path:
    """Recents, positions and settings."""
    return _ensure(_base("XDG_DATA_HOME", ".local/share") / APP_DIR_NAME)


def cache_dir() -> Path:
    """Rendered pages, thumbnails and the engine's private profile: safe to delete at any time."""
    return _ensure(_base("XDG_CACHE_HOME", ".cache") / APP_DIR_NAME)


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path

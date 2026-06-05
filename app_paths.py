import os
import sys
from pathlib import Path


APP_NAME = "KappalRateCapture"


def bundled_base_dir() -> Path:
    """Directory containing bundled read-only assets."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def app_data_dir() -> Path:
    """Writable per-user app data directory."""
    override = os.environ.get("KAPPAL_APP_DATA_DIR")
    if override:
        root = Path(override)
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def runtime_dir(name: str) -> Path:
    path = app_data_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_file(name: str) -> Path:
    return app_data_dir() / name

"""Local Windows application paths."""
import ctypes
import os
from pathlib import Path


def data_dir() -> Path:
    path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GigaAMZoom"
    path.mkdir(parents=True, exist_ok=True)
    return path


def local_directory(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir() or str(value).startswith(("\\\\", "//")) or str(path).startswith(("\\\\", "//")):
        raise ValueError("Выберите существующую папку на локальном диске.")
    if os.name == "nt" and ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
        raise ValueError("Сетевой диск не подходит: выберите локальную папку.")
    return path

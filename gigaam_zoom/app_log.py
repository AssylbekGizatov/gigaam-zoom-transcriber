"""Local JSON-lines diagnostics. Accept only operational fields and no free-form messages."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import platform
import re
import sys
import threading
import traceback
import uuid

from . import __version__
from .build_revision import ARCHIVE_REVISION

_ROOT = Path(__file__).resolve().parent.parent
_lock = threading.RLock()
_logger = logging.Logger("gigaam_zoom.local", logging.INFO)
_logger.propagate = False
_session = uuid.uuid4().hex
_directory: Path | None = None
_verbose = False
_CODE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+\Z")
_LANGUAGE = re.compile(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")


def log_directory() -> Path:
    return _directory or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GigaAMZoom" / "logs"


def configure_logging(*, verbose: bool = False, directory: Path | None = None) -> Path:
    """Initialize the parent process writer. Keep current app.log plus four backups."""
    global _directory, _verbose
    with _lock:
        target = Path(directory) if directory is not None else log_directory()
        target.mkdir(parents=True, exist_ok=True)
        path = target / "app.log"
        if _directory != target or not _logger.handlers:
            handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=4, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            for old in _logger.handlers[:]:
                _logger.removeHandler(old)
                old.close()
            _logger.addHandler(handler)
            _directory = target
        _verbose = verbose
        _write("app_start", {"python": platform.python_version(), "app_version": __version__,
                             "commit": _commit_sha(), "windows_build": _windows_build()})
        return path


def set_verbose_logging(enabled: bool) -> None:
    global _verbose
    with _lock:
        _verbose = bool(enabled)
        _write("log_verbosity", {"enabled": _verbose})


def verbose_logging_enabled() -> bool:
    with _lock:
        return _verbose


def _write(event: str, fields: dict) -> None:
    # Never implicitly create a writer in spawned OCR workers or callers' tests.
    if not _logger.handlers:
        return
    record = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
              "session": _session, "event": event, **fields}
    _logger.info(json.dumps(record, ensure_ascii=False, allow_nan=False))


def _hide_home(text: str) -> str:
    paths = {str(Path.home()), os.environ.get("USERPROFILE", "")}
    for path in paths:
        if path and len(path) > 2:
            text = re.sub(re.escape(path), "%USERPROFILE%", text, flags=re.IGNORECASE)
    return text


def log_event(event: str, **fields) -> None:
    """event/evidence/decision are static machine codes, never user-supplied strings."""
    if not isinstance(event, str) or not _CODE.fullmatch(event):
        return
    allowed = {}
    for key, value in fields.items():
        if key in {"evidence", "decision", "reason"}:
            if isinstance(value, str) and _CODE.fullmatch(value) and len(value) <= 80:
                allowed[key] = value
        elif key == "source" and value in ("remote", "microphone", "loopback"):
            allowed[key] = value
        elif key == "format" and value in ("PDF", "DOCX", "TXT"):
            allowed[key] = value
        elif key == "language" and isinstance(value, str) and _LANGUAGE.fullmatch(value):
            allowed[key] = value
        elif key in {"model_ok", "enabled", "microphone_enabled", "calibrated"} and type(value) is bool:
            allowed[key] = value
        elif key in {"device_index", "loopback_index", "microphone_index", "capture_ms", "ocr_ms",
                     "max_age_ms", "line_count", "word_count", "dpi", "monitor_index", "count"}:
            if type(value) in (int, float) and math.isfinite(value):
                allowed[key] = value
        elif key in {"client_rect", "image_size"} and isinstance(value, (tuple, list)):
            if len(value) in (2, 4) and all(type(item) is int for item in value):
                allowed[key] = list(value)
    with _lock:
        if _verbose and event == "ocr_observation" and isinstance(fields.get("ocr_lines"), (list, tuple)):
            allowed["ocr_lines"] = [_hide_home(line)[:500] for line in fields["ocr_lines"][:24]
                                    if isinstance(line, str)]
        _write(event, allowed)


def log_ocr_frame(frame) -> None:
    """Allowlist frame telemetry at the parent boundary. Never serialize the frame."""
    log_event("ocr_observation", evidence=frame.observation.evidence, decision=frame.parser_reason,
              capture_ms=frame.timings.get("capture_ms"), ocr_ms=frame.timings.get("ocr_ms"),
              max_age_ms=frame.max_age_ms, line_count=frame.line_count, word_count=frame.word_count,
              client_rect=frame.window.get("client_rect"), image_size=frame.image_size,
              dpi=frame.window.get("dpi"), language=frame.language, ocr_lines=frame.raw_lines)
    # The worker only sends exception_details(), never an exception repr/message.
    info = getattr(frame, "error_info", None)
    if isinstance(info, dict) and set(info) == {"error_type", "stack"}:
        with _lock:
            _write("ocr_backend_error", info)


def exception_details(exc: BaseException) -> dict:
    """Keep traceback locations. Exception messages/source snippets can contain user data."""
    frames = []
    for frame, line in traceback.walk_tb(exc.__traceback__):
        module = frame.f_globals.get("__name__", "")
        # No filenames from arbitrary exceptions/user modules. Describe call stack by
        # trusted application module + source line, or a generic external frame.
        if not isinstance(module, str) or not re.fullmatch(r"gigaam_zoom(?:\.[a-z_]+)+", module):
            module = "external"
        frames.append({"module": module, "line": line})
    return {"error_type": type(exc).__name__, "stack": frames[-48:]}


def log_exception(event: str, exc: BaseException) -> None:
    if isinstance(event, str) and _CODE.fullmatch(event):
        with _lock:
            _write(event, exception_details(exc))


def _commit_sha() -> str | None:
    # Git archives substitute this at export. Development checkouts don't require git.exe.
    if _SHA.fullmatch(ARCHIVE_REVISION):
        return ARCHIVE_REVISION
    try:
        dotgit = _ROOT / ".git"
        head = (dotgit / "HEAD").read_text(encoding="ascii").strip()
        if head.startswith("ref: refs/") and ".." not in head:
            ref = head[5:]
            try:
                head = (dotgit / ref).read_text(encoding="ascii").strip()
            except FileNotFoundError:
                head = next((line.split()[0] for line in (dotgit / "packed-refs").read_text().splitlines()
                             if line.endswith(" " + ref)), "")
        return head if _SHA.fullmatch(head) else None
    except (ValueError, OSError, UnicodeError):
        return None


def _windows_build() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg
    version = sys.getwindowsversion()
    build = f"{version.major}.{version.minor}.{version.build}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
            revision, _ = winreg.QueryValueEx(key, "UBR")
        return f"{build}.{int(revision)}"
    except (ValueError, OSError):
        return build


def _zoom_version() -> str | None:
    """Read the running executable's numeric version resource. Never retain its path."""
    import ctypes
    from ctypes import wintypes as w
    import psutil

    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [w.LPCWSTR, ctypes.POINTER(w.DWORD)]
    version.GetFileVersionInfoSizeW.restype = w.DWORD
    version.GetFileVersionInfoW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID]
    version.VerQueryValueW.argtypes = [w.LPCVOID, w.LPCWSTR, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.UINT)]
    for proc in psutil.process_iter(["name"]):
        if (proc.info["name"] or "").casefold() != "zoom.exe":
            continue
        try:
            path = proc.exe()
        except psutil.Error:
            continue
        size = version.GetFileVersionInfoSizeW(path, None)
        if not 0 < size < 10_000_000:
            continue
        data = ctypes.create_string_buffer(size)
        address, length = w.LPVOID(), w.UINT()
        if not (version.GetFileVersionInfoW(path, 0, size, data)
                and version.VerQueryValueW(data, "\\", ctypes.byref(address), ctypes.byref(length))
                and address and length.value >= 52):
            continue
        fixed = ctypes.cast(address, ctypes.POINTER(w.DWORD))
        if fixed[0] == 0xFEEF04BD:
            high, low = fixed[2], fixed[3]
            return f"{high >> 16}.{high & 65535}.{low >> 16}.{low & 65535}"
    return None


def log_startup_environment() -> None:
    """Call off the Tk thread, after enabling per-monitor DPI awareness."""
    if sys.platform != "win32":
        return
    details = {}
    try:
        from .zoom_window import list_monitors
        details["monitors"] = [{"bounds": list(m.bounds), "dpi": m.dpi} for m in list_monitors()]
    except Exception as exc:
        log_exception("monitor_info_error", exc)
    try:
        from .zoom_visual import installed_ocr_languages
        details["ocr_languages"] = [tag for tag in installed_ocr_languages()
                                    if isinstance(tag, str) and _LANGUAGE.fullmatch(tag)]
    except Exception as exc:
        log_exception("ocr_languages_error", exc)
    try:
        details["zoom_version"] = _zoom_version()
    except Exception as exc:
        log_exception("zoom_version_error", exc)
    with _lock:
        _write("windows_environment", details)

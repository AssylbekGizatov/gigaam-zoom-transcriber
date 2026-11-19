"""Local JSON-lines diagnostics. Accept only operational fields and no free-form messages."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import blake2s
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

def _scrub_fault_stream(source, sink):
    # faulthandler writes bytes to a file descriptor; a Python stream wrapper
    # in the crashing process cannot intercept its absolute source filenames.
    fault_kinds = ("access violation", "segmentation fault", "stack overflow", "aborted",
                   "illegal instruction", "floating point exception", "bus error")
    for raw in source:
        text = raw[:4096].decode("ascii", "replace").strip()
        if text.lower().startswith(("windows fatal exception:", "fatal python error:")):
            kind = next((kind for kind in fault_kinds if kind in text.lower()), "native exception")
            safe = f"Native fault: {kind}"
        elif text.startswith(("Current thread ", "Thread ", "Stack (most recent call first)")):
            safe = "Thread stack (most recent call first):"
        elif match := re.fullmatch(r'File "(.+)", line ([0-9]+) in (.*)', text):
            source_file, line, function = match.groups()
            source_file = source_file.replace("\\", "/")
            stem = source_file.rsplit("/", 1)[-1]
            trusted = ("/gigaam_zoom/" in source_file or stem == "app.py") and re.fullmatch(r"[a-z_]+\.py", stem)
            location = stem if trusted else "external"
            function = function if trusted and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", function) else ""
            safe = f"  {location}:{line} {function}".rstrip()
        else:
            continue  # Omit arbitrary native error messages, module lists and user text.
        sink.write((safe + "\n").encode("ascii"))
        sink.flush()


if __name__ == "__main__" and sys.argv[1:] == ["--scrub-fault-stream"]:
    _scrub_fault_stream(sys.stdin.buffer, sys.stdout.buffer)
    raise SystemExit(0)


from . import __version__
from .build_revision import ARCHIVE_REVISION

_ROOT = Path(__file__).resolve().parent.parent
_lock = threading.RLock()
_logger = logging.Logger("gigaam_zoom.local", logging.INFO)
_logger.propagate = False
_session = uuid.uuid4().hex
_alias_key = os.urandom(16)
_directory: Path | None = None
_crash_file = None
_crash_relay = None
_verbose = False
_CODE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+\Z")
_LANGUAGE = re.compile(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SEGMENT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_CAPTURE_METHODS = {"wgc", "printwindow", "screen_dc"}


def log_directory() -> Path:
    return _directory or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GigaAMZoom" / "logs"


def enable_crash_logging(*, directory: Path | None = None) -> Path:
    """Install before native imports. Write breadcrumbs and scrubbed stacks to one stable file."""
    import atexit
    import faulthandler
    import subprocess
    global _crash_file, _crash_relay
    with _lock:
        if _crash_file is not None:
            return Path(_crash_file.name)
        folder = Path(directory) if directory is not None else log_directory()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "crash.log"
        out = path.open("ab", buffering=0)
        relay = None
        try:
            # A separate, unbuffered stdlib interpreter survives a fault in this
            # process. Closing all writers (including on OS process exit) sends EOF.
            # Only scrubbed lines reach disk. O_APPEND keeps parent breadcrumbs intact.
            relay = subprocess.Popen([sys.executable, "-I", "-S", "-u", str(Path(__file__).resolve()),
                                      "--scrub-fault-stream"], stdin=subprocess.PIPE, stdout=out,
                                     stderr=subprocess.DEVNULL, bufsize=0,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            faulthandler.enable(file=relay.stdin, all_threads=True)
        except BaseException:
            if relay is not None:
                relay.stdin.close()
                relay.wait(timeout=3)
            out.close()
            raise
        _crash_file, _crash_relay = out, relay
        atexit.register(_close_crash_logging)
        return path


def _close_crash_logging():
    import faulthandler
    import subprocess
    global _crash_file, _crash_relay
    if _crash_file is None:
        return
    faulthandler.disable()
    _crash_relay.stdin.close()
    try:
        _crash_relay.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass  # The relay owns its duplicated output handle until it consumes EOF.
    _crash_file.close()
    _crash_file = _crash_relay = None


def breadcrumb(step: str) -> None:
    """Flush a fixed step code before the caller enters the next startup operation."""
    if not isinstance(step, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", step):
        return
    with _lock:
        if _crash_file is not None:
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            _crash_file.write(f"{now} {_session} {step}\n".encode("ascii"))
            _crash_file.flush()
        _write("startup_step", {"step": step})
        for handler in _logger.handlers:
            handler.flush()


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
            # Windows accepts either separator, including in text pasted into Zoom.
            pattern = r"[\\/]".join(re.escape(part) for part in re.split(r"[\\/]", path))
            text = re.sub(pattern, "%USERPROFILE%", text, flags=re.IGNORECASE)
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
        elif key in {"segment_id", "left_anchor_id", "right_anchor_id"} and isinstance(value, str) and _SEGMENT_ID.fullmatch(value):
            allowed[key] = value
        elif key == "capture_method" and value in _CAPTURE_METHODS:
            allowed[key] = value
        elif key == "capture_attempts" and isinstance(value, (list, tuple)):
            allowed[key] = [{"method": a["method"], "evidence": a["evidence"]} for a in value[:3]
                            if isinstance(a, dict) and a.get("method") in _CAPTURE_METHODS
                            and isinstance(a.get("evidence"), str)
                            and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", a["evidence"])]
        elif key in {"model_ok", "enabled", "microphone_enabled", "calibrated"} and type(value) is bool:
            allowed[key] = value
        elif key in {"device_index", "loopback_index", "microphone_index", "capture_ms", "ocr_ms",
                     "max_age_ms", "line_count", "word_count", "dpi", "monitor_index", "count"}:
            if type(value) in (int, float) and math.isfinite(value):
                allowed[key] = value
        elif key in {"exit_code", "retry_count", "retry_delay_ms"} and type(value) is int:
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
              capture_method=getattr(frame, "capture_method", ""), capture_attempts=getattr(frame, "capture_attempts", ()),
              capture_ms=frame.timings.get("capture_ms"), ocr_ms=frame.timings.get("ocr_ms"),
              max_age_ms=frame.max_age_ms, line_count=frame.line_count, word_count=frame.word_count,
              client_rect=frame.window.get("client_rect"), image_size=frame.image_size,
              dpi=frame.window.get("dpi"), language=frame.language, ocr_lines=frame.raw_lines)
    # The worker only sends exception_details(), never an exception repr/message.
    info = getattr(frame, "error_info", None)
    if isinstance(info, dict) and set(info) == {"error_type", "stack"}:
        with _lock:
            _write("ocr_backend_error", info)


def log_attribution(window_start_ms, window_end_ms, candidates, chosen, reason, *, segment_id=None):
    """Record weighted OCR evidence on the capture clock. Real names require explicit opt-in."""
    def label(name):
        if _verbose:
            return _hide_home(name)[:512]
        digest = blake2s(name.encode("utf-8"), key=_alias_key, digest_size=8).hexdigest()
        return "speaker-" + digest

    if not all(isinstance(t, (int, float)) and math.isfinite(t) for t in (window_start_ms, window_end_ms)):
        return
    with _lock:
        fields = {"window_ms": [round(window_start_ms), round(window_end_ms)],
                  "candidates": [{"speaker": label(name), "weighted_ms": round(weight, 2)}
                                 for name, weight in candidates if isinstance(name, str) and name
                                 and isinstance(weight, (int, float)) and math.isfinite(weight) and weight >= 0],
                  "chosen": label(chosen) if isinstance(chosen, str) and chosen else None,
                  "reason": reason if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason)
                  else "unknown_reason"}
        if isinstance(segment_id, str) and _SEGMENT_ID.fullmatch(segment_id):
            fields["segment_id"] = segment_id
        _write("attribution_decision", fields)


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


_WINDOW_CODES = {"zoom_window_detected", "zoom_window_not_found", "zoom_window_ambiguous",
                 "ocr_window_minimized", "ocr_window_too_small", "ocr_window_unavailable",
                 "ocr_window_changed", "ocr_windows_only", "ocr_focus_failed", "ocr_dpi_unavailable"}
_ENV_ERRORS = {"monitor_info_error", "ocr_languages_error", "zoom_version_error", "zoom_window_check_error"}


def collect_startup_environment() -> dict:
    """Isolated probe entry point. Native operations must only run in a disposable process."""
    details = {"ocr_languages": [], "monitors": [], "zoom_version": None,
               "zoom_window": {"found": False, "evidence": "ocr_windows_only"}, "errors": []}
    if sys.platform != "win32":
        return details
    from .zoom_window import find_meeting_window, list_monitors, WindowProbeError
    try:
        details["monitors"] = [{"bounds": list(m.bounds), "dpi": m.dpi} for m in list_monitors()]
    except Exception:
        details["errors"].append("monitor_info_error")
    try:
        from .zoom_visual import installed_ocr_languages
        details["ocr_languages"] = [tag for tag in installed_ocr_languages() if _LANGUAGE.fullmatch(tag)]
    except Exception:
        details["errors"].append("ocr_languages_error")
    try:
        details["zoom_version"] = _zoom_version()
    except Exception:
        details["errors"].append("zoom_version_error")
    try:
        window = find_meeting_window()
        details["zoom_window"] = {"found": True, "evidence": "zoom_window_detected",
                                  "client_rect": list(window.client_bounds), "dpi": window.dpi}
    except WindowProbeError as exc:
        code = exc.code if exc.code in _WINDOW_CODES else "ocr_window_unavailable"
        details["zoom_window"] = {"found": False, "evidence": code}
    except Exception:
        details["zoom_window"] = {"found": False, "evidence": "ocr_window_unavailable"}
        details["errors"].append("zoom_window_check_error")
    return details


def log_windows_environment(details: dict) -> None:
    """Parent-side allowlist. The child never opens this process's rotating writer."""
    def valid_rect(value):
        return isinstance(value, (tuple, list)) and len(value) == 4 and all(type(v) is int for v in value)

    if not isinstance(details, dict):
        return
    safe = {}
    for key in ("ocr_languages", "errors", "monitors"):
        values = details.get(key)
        if not isinstance(values, (list, tuple)):
            continue
        if key == "ocr_languages":
            safe[key] = [tag for tag in values[:128] if isinstance(tag, str) and _LANGUAGE.fullmatch(tag)]
        elif key == "errors":
            safe[key] = [code for code in values[:16] if isinstance(code, str) and code in _ENV_ERRORS]
        else:
            safe[key] = [{"bounds": m["bounds"], "dpi": m["dpi"]} for m in values[:64]
                         if isinstance(m, dict) and valid_rect(m.get("bounds")) and type(m.get("dpi")) is int]
    version = details.get("zoom_version")
    safe["zoom_version"] = version if isinstance(version, str) and re.fullmatch(r"\d+(?:\.\d+){1,3}", version) else None
    window = details.get("zoom_window")
    if isinstance(window, dict) and isinstance(window.get("evidence"), str) and window["evidence"] in _WINDOW_CODES:
        safe["zoom_window"] = {"evidence": window["evidence"], "found": window.get("found") is True}
        if valid_rect(window.get("client_rect")) and type(window.get("dpi")) is int:
            safe["zoom_window"].update(client_rect=window["client_rect"], dpi=window["dpi"])
    with _lock:
        _write("windows_environment", safe)

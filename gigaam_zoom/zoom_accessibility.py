"""Optional, read-only hints from explicit Zoom accessibility speaker labels.

These are UI observations, NOT Zoom audio source IDs or guaranteed attribution.
No participant-list name, selected/pinned video, or arbitrary window title is
treated as a speaker. Missing/ambiguous/stale observations fail closed. UIA can
block inside another application, so all COM calls run in a killable process.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import multiprocessing as mp
import os
import re
import sys
import threading
import time
import unicodedata
import uuid


UNKNOWN_SOURCE_ID = "local:zoom-uia:unknown"


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class SpeakerObservation:
    observed_at_ms: int
    source_id: str
    display_name: str | None
    evidence: str


@dataclass(frozen=True, slots=True)
class AccessibleLabel:
    """One accessible element; identity is local to this UI session only."""

    identity: str
    name: str


_PATTERNS = (
    re.compile(r"Active speaker\s*:\s*(?P<name>.+)", re.IGNORECASE),
    re.compile(r"Speaking\s*:\s*(?P<name>.+)", re.IGNORECASE),
    re.compile(r"(?P<name>.+) is speaking", re.IGNORECASE),
    re.compile(r"Активный говорящий\s*:\s*(?P<name>.+)", re.IGNORECASE),
    re.compile(r"Говорит\s*:\s*(?P<name>.+)", re.IGNORECASE),
    re.compile(r"Сейчас говорит\s*:\s*(?P<name>.+)", re.IGNORECASE),
)


def parse_active_speaker_label(label: str) -> str | None:
    """Strict allowlist, deliberately not a heuristic for generic name tiles.

    Label variants are adapter contracts, not a claim that every Zoom release
    exposes them. Unsupported client language/version simply returns unknown.
    """
    if not isinstance(label, str) or len(label) > 240:
        return None
    # Newlines/control characters usually mean a compound toolbar/tree label.
    if any(unicodedata.category(char).startswith("C") for char in label):
        return None
    text = unicodedata.normalize("NFC", label).strip()
    for pattern in _PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            name = match.group("name").strip()
            if 0 < len(name) <= 160 and name not in {"...", "…", "-", "—"}:
                return name
    return None


def observation_from_labels(
    labels: list[AccessibleLabel], observed_at_ms: int, *, complete: bool = True,
) -> SpeakerObservation:
    """Require exactly one explicit speaker element in a complete scan."""
    if not complete:
        return SpeakerObservation(observed_at_ms, UNKNOWN_SOURCE_ID, None, "uia_scan_incomplete")
    matches: dict[str, str] = {}
    for label in labels:
        name = parse_active_speaker_label(label.name)
        if name is not None and label.identity:
            previous = matches.get(label.identity)
            if previous is not None and previous != name:
                return SpeakerObservation(observed_at_ms, UNKNOWN_SOURCE_ID, None, "uia_ambiguous")
            matches[label.identity] = name
    if len(matches) != 1:
        reason = "uia_missing" if not matches else "uia_ambiguous"
        return SpeakerObservation(observed_at_ms, UNKNOWN_SOURCE_ID, None, reason)
    identity, name = next(iter(matches.items()))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return SpeakerObservation(observed_at_ms, f"local:zoom-uia:{digest}", name,
                              "uia_explicit_active_speaker_hint")


def _zoom_windows() -> list[tuple[int, int]]:
    """Enumerate visible top-level windows owned specifically by Zoom.exe."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                  wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    matches: list[tuple[int, int]] = []

    @callback_type
    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return True
        try:
            size = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                if os.path.basename(path.value).casefold() == "zoom.exe":
                    matches.append((int(hwnd), pid.value))
        finally:
            kernel32.CloseHandle(handle)
        # Cap enumeration output; the caller detects an incomplete window set.
        return len(matches) < 13

    user32.EnumWindows(visit, 0)
    return matches


def _scan_uia(automation, session_id: str) -> SpeakerObservation:
    started = time.monotonic()
    observed = monotonic_ms()
    windows = _zoom_windows()
    labels: list[AccessibleLabel] = []
    if len(windows) > 12:
        return observation_from_labels(labels, observed, complete=False)
    walker = automation.ControlViewWalker
    count = 0
    for hwnd, pid in windows:
        root = automation.ElementFromHandle(hwnd)
        pending = [(root, 0)]
        while pending:
            if count >= 512 or time.monotonic() - started > 0.8:
                return observation_from_labels(labels, observed, complete=False)
            element, depth = pending.pop()
            count += 1
            # Never read elements exposed by another process (embedded webviews
            # require an explicitly supported adapter, not a global tree walk).
            if element.CurrentProcessId != pid or element.CurrentIsOffscreen:
                continue
            name = element.CurrentName or ""
            if parse_active_speaker_label(name) is not None:
                runtime = tuple(element.GetRuntimeId())
                if runtime:
                    identity = f"{session_id}:{pid}:{hwnd}:{runtime}"
                    labels.append(AccessibleLabel(identity, name))
            child = walker.GetFirstChildElement(element)
            if child and depth >= 12:
                return observation_from_labels(labels, observed, complete=False)
            while child:
                if len(pending) + count >= 512 or time.monotonic() - started > 0.8:
                    return observation_from_labels(labels, observed, complete=False)
                pending.append((child, depth + 1))
                child = walker.GetNextSiblingElement(child)
    return observation_from_labels(labels, observed)


def _uia_worker(connection, session_id: str) -> None:
    """Runs only in a spawned process. Does not log UI text or exception data."""
    automation = None
    try:
        if os.name == "nt":
            try:
                # comtypes initializes COM on import using this flag. The child
                # owns no windows; Microsoft recommends MTA for UIA clients.
                sys.coinit_flags = 0
                import comtypes.client
                module = comtypes.client.GetModule("UIAutomationCore.dll")
                automation = comtypes.client.CreateObject(module.CUIAutomation,
                                                         interface=module.IUIAutomation)
            except Exception:
                automation = None
        while True:
            command = connection.recv()
            if command != "sample":
                return
            if automation is None:
                result = SpeakerObservation(monotonic_ms(), UNKNOWN_SOURCE_ID, None, "uia_unavailable")
            else:
                try:
                    result = _scan_uia(automation, session_id)
                except Exception:
                    # A destroyed window or provider error invalidates the whole
                    # snapshot; never reuse a previously observed speaker name.
                    result = SpeakerObservation(monotonic_ms(), UNKNOWN_SOURCE_ID, None, "uia_unavailable")
            connection.send(result)
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


class ZoomSpeakerObserver:
    """Background UI hints. ``latest`` is immediate and safe on the GUI thread.

    ``observed_at_ms`` is the system monotonic clock, matching capture clocks
    based on ``time.monotonic``. Callers must not apply a later name to earlier
    buffered audio. No network calls or Zoom settings changes are performed.
    """

    def __init__(self, *, poll_interval: float = 0.75, max_age_ms: int = 2000,
                 probe_timeout: float = 2.5, _context=None, _worker=None):
        if poll_interval < 0.5 or max_age_ms <= 0 or probe_timeout <= 0:
            raise ValueError("Polling must be at most 2 Hz; timeouts must be positive")
        self.poll_interval = poll_interval
        self.max_age_ms = max_age_ms
        self.probe_timeout = probe_timeout
        self._context = _context or mp.get_context("spawn")
        self._worker = _worker or _uia_worker
        self._session_id = uuid.uuid4().hex
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._latest = SpeakerObservation(monotonic_ms(), UNKNOWN_SOURCE_ID, None, "uia_not_started")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="zoom-speaker-observer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout))
        with self._lock:
            self._latest = SpeakerObservation(monotonic_ms(), UNKNOWN_SOURCE_ID, None, "uia_stopped")

    def latest(self, now_ms: int | None = None) -> SpeakerObservation:
        now = monotonic_ms() if now_ms is None else now_ms
        with self._lock:
            result = self._latest
        age = now - result.observed_at_ms
        if age < 0 or age > self.max_age_ms:
            return SpeakerObservation(now, UNKNOWN_SOURCE_ID, None, "uia_stale")
        return result

    def _publish(self, result: SpeakerObservation) -> None:
        with self._lock:
            if not self._stop.is_set():
                self._latest = result

    def _run(self) -> None:
        process = None
        connection = None
        try:
            while not self._stop.is_set():
                cycle_started = time.monotonic()
                try:
                    if process is None:
                        connection, child = self._context.Pipe(duplex=True)
                        process = self._context.Process(target=self._worker,
                                                        args=(child, self._session_id), daemon=True)
                        process.start()
                        child.close()
                    connection.send("sample")
                    deadline = time.monotonic() + self.probe_timeout
                    result = None
                    while not self._stop.is_set() and time.monotonic() < deadline:
                        if connection.poll(0.05):
                            result = connection.recv()
                            break
                    if self._stop.is_set():
                        break
                    if not isinstance(result, SpeakerObservation):
                        self._publish(SpeakerObservation(monotonic_ms(), UNKNOWN_SOURCE_ID, None, "uia_timeout"))
                        self._dispose(process, connection)
                        process = connection = None
                    else:
                        self._publish(result)
                except (OSError, EOFError, RuntimeError, ValueError):
                    self._publish(SpeakerObservation(monotonic_ms(), UNKNOWN_SOURCE_ID, None, "uia_unavailable"))
                    self._dispose(process, connection)
                    process = connection = None
                self._stop.wait(max(0, self.poll_interval - (time.monotonic() - cycle_started)))
        finally:
            self._dispose(process, connection)

    @staticmethod
    def _dispose(process, connection) -> None:
        if connection is not None:
            connection.close()
        if process is not None:
            if process.pid is None:
                process.close()
                return
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.5)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.5)
            if not process.is_alive():
                process.close()

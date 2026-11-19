"""Native Zoom window discovery and screen geometry in physical pixels.

Discovery is repeated for each sample. A unique visible Zoom.exe meeting is
required; foreground focus is not. A capture is allowed only when the entire
region is on a monitor and no visible window above Zoom intersects it.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
import ntpath
import os

Bounds = tuple[int, int, int, int]


class WindowProbeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MeetingWindow:
    hwnd: int
    pid: int
    title: str
    class_name: str
    client_bounds: Bounds
    monitor_id: str
    dpi: int


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    device: str
    bounds: Bounds
    work_bounds: Bounds
    dpi: int


def _meeting_rank(title: str, class_name: str) -> int:
    """Main meeting roots outrank auxiliary video surfaces, in any UI language.

    Zoom's native class prefix remains useful when the localized/custom meeting
    title changes. Generic Zoom ownership or an arbitrary large window alone
    never qualifies. Explicit home/toolbar identities override other hints.
    """
    title, class_name = title.strip().casefold(), class_name.casefold()
    if (title in {"zoom workplace", "zoom cloud meetings"}
            or class_name.startswith(("zptoolbar", "zphostwnd", "zphome", "zpmainframe"))):
        return 0
    if class_name.startswith(("zpfloatvideo", "zpvideoview", "zpvideownd")):
        return 1
    if (class_name.startswith(("zpcontentviewwnd", "zpmeetingwnd", "zpmeetingview"))
            or title in {"zoom meeting", "конференция zoom", "zoom-встреча",
                         "reunión de zoom", "réunion zoom", "zoom-meeting"}):
        return 2
    return 0


def is_meeting_window(title: str, class_name: str) -> bool:
    return _meeting_rank(title, class_name) > 0


@dataclass(frozen=True, slots=True)
class _Candidate:
    hwnd: int
    title: str
    class_name: str
    bounds: Bounds
    rank: int
    minimized: bool

    @property
    def area(self) -> int:
        return max(0, self.bounds[2] - self.bounds[0]) * max(0, self.bounds[3] - self.bounds[1])


def intersection(first: Bounds, second: Bounds) -> Bounds | None:
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    return (left, top, right, bottom) if left < right and top < bottom else None


def covered_by_monitors(region: Bounds, monitors: list[Bounds]) -> bool:
    """Require coverage of every pixel, including gaps between monitors."""
    if region[0] >= region[2] or region[1] >= region[3]:
        return False
    remaining = [region]
    for monitor in monitors:
        next_remaining = []
        for part in remaining:
            cut = intersection(part, monitor)
            if cut is None:
                next_remaining.append(part)
                continue
            left, top, right, bottom = part
            x1, y1, x2, y2 = cut
            next_remaining.extend(rect for rect in (
                (left, top, right, y1), (left, y2, right, bottom),
                (left, y1, x1, y2), (x2, y1, right, y2),
            ) if rect[0] < rect[2] and rect[1] < rect[3])
        remaining = next_remaining
        if not remaining:
            return True
    return False


class _Win32:
    """Small native adapter; all public policy helpers are mockable on Linux."""

    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        self.user = c.WinDLL("user32", use_last_error=True)
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.dwm = c.WinDLL("dwmapi", use_last_error=True)
        self.gdi = c.WinDLL("gdi32", use_last_error=True)
        self.shcore = c.WinDLL("shcore", use_last_error=True)
        self.enum_callback = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        self.monitor_callback = c.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC, c.POINTER(w.RECT), w.LPARAM)
        class MonitorInfoEx(c.Structure):
            _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT),
                        ("dwFlags", w.DWORD), ("szDevice", w.WCHAR * 32)]
        self.monitor_type = MonitorInfoEx
        def bind(dll, name, result, *args):
            function = getattr(dll, name)
            function.restype, function.argtypes = result, list(args)
        for name in ("IsWindow", "IsWindowVisible", "IsIconic", "SetForegroundWindow"):
            bind(self.user, name, w.BOOL, w.HWND)
        bind(self.user, "EnumWindows", w.BOOL, self.enum_callback, w.LPARAM)
        bind(self.user, "EnumDisplayMonitors", w.BOOL, w.HDC, c.POINTER(w.RECT), self.monitor_callback, w.LPARAM)
        bind(self.user, "GetMonitorInfoW", w.BOOL, w.HANDLE, c.POINTER(MonitorInfoEx))
        bind(self.user, "GetWindow", w.HWND, w.HWND, w.UINT)
        bind(self.user, "GetWindowRect", w.BOOL, w.HWND, c.POINTER(w.RECT))
        bind(self.user, "GetClientRect", w.BOOL, w.HWND, c.POINTER(w.RECT))
        bind(self.user, "ClientToScreen", w.BOOL, w.HWND, c.POINTER(w.POINT))
        bind(self.user, "GetWindowTextW", c.c_int, w.HWND, w.LPWSTR, c.c_int)
        bind(self.user, "GetClassNameW", c.c_int, w.HWND, w.LPWSTR, c.c_int)
        bind(self.user, "GetWindowThreadProcessId", w.DWORD, w.HWND, c.POINTER(w.DWORD))
        bind(self.user, "MonitorFromWindow", w.HANDLE, w.HWND, w.DWORD)
        bind(self.user, "GetDpiForWindow", w.UINT, w.HWND)
        bind(self.user, "SetThreadDpiAwarenessContext", w.HANDLE, w.HANDLE)
        bind(self.user, "SetProcessDpiAwarenessContext", w.BOOL, w.HANDLE)
        bind(self.user, "GetThreadDpiAwarenessContext", w.HANDLE)
        bind(self.user, "AreDpiAwarenessContextsEqual", w.BOOL, w.HANDLE, w.HANDLE)
        bind(self.user, "ShowWindow", w.BOOL, w.HWND, c.c_int)
        bind(self.user, "GetForegroundWindow", w.HWND)
        bind(self.user, "GetAncestor", w.HWND, w.HWND, w.UINT)
        bind(self.user, "GetWindowRgn", c.c_int, w.HWND, w.HANDLE)
        bind(self.user, "GetLayeredWindowAttributes", w.BOOL, w.HWND, c.POINTER(w.DWORD),
             c.POINTER(w.BYTE), c.POINTER(w.DWORD))
        bind(self.kernel, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
        bind(self.kernel, "QueryFullProcessImageNameW", w.BOOL, w.HANDLE, w.DWORD,
             w.LPWSTR, c.POINTER(w.DWORD))
        bind(self.kernel, "CloseHandle", w.BOOL, w.HANDLE)
        bind(self.dwm, "DwmGetWindowAttribute", c.c_long, w.HWND, w.DWORD, c.c_void_p, w.DWORD)
        bind(self.gdi, "CreateRectRgn", w.HANDLE, c.c_int, c.c_int, c.c_int, c.c_int)
        bind(self.gdi, "OffsetRgn", c.c_int, w.HANDLE, c.c_int, c.c_int)
        bind(self.gdi, "RectInRegion", w.BOOL, w.HANDLE, c.POINTER(w.RECT))
        bind(self.gdi, "DeleteObject", w.BOOL, w.HANDLE)
        bind(self.shcore, "GetDpiForMonitor", c.c_long, w.HANDLE, c.c_int,
             c.POINTER(w.UINT), c.POINTER(w.UINT))

    def enable_process_dpi(self):
        context = self.c.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        return bool(self.user.SetProcessDpiAwarenessContext(context) or
                    self.user.AreDpiAwarenessContextsEqual(self.user.GetThreadDpiAwarenessContext(), context))

    def set_thread_dpi(self, context):
        return self.user.SetThreadDpiAwarenessContext(self.c.c_void_p(context))

    def handles(self):
        handles = []
        @self.enum_callback
        def collect(hwnd, _):
            handles.append(int(hwnd))
            return True
        if not self.user.EnumWindows(collect, 0):
            raise WindowProbeError("ocr_window_unavailable")
        return handles

    def visible(self, hwnd):
        return bool(self.user.IsWindowVisible(hwnd))

    def minimized(self, hwnd):
        return bool(self.user.IsIconic(hwnd))

    def cloaked(self, hwnd):
        value = self.w.DWORD()
        status = self.dwm.DwmGetWindowAttribute(hwnd, 14, self.c.byref(value), self.c.sizeof(value))
        if status < 0:
            raise WindowProbeError("ocr_window_changed")
        return bool(value.value)

    def metadata(self, hwnd):
        title, name = self.c.create_unicode_buffer(512), self.c.create_unicode_buffer(256)
        self.user.GetWindowTextW(hwnd, title, len(title))
        if not self.user.GetClassNameW(hwnd, name, len(name)):
            raise WindowProbeError("ocr_window_changed")
        return title.value, name.value

    def pid(self, hwnd):
        pid = self.w.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, self.c.byref(pid))
        return pid.value

    def process_path(self, hwnd):
        handle = self.kernel.OpenProcess(0x1000, False, self.pid(hwnd))
        if not handle:
            return ""
        try:
            size = self.w.DWORD(32768)
            path = self.c.create_unicode_buffer(size.value)
            return path.value if self.kernel.QueryFullProcessImageNameW(handle, 0, path, self.c.byref(size)) else ""
        finally:
            self.kernel.CloseHandle(handle)

    def client_bounds(self, hwnd):
        rect, origin = self.w.RECT(), self.w.POINT()
        if not (self.user.GetClientRect(hwnd, self.c.byref(rect)) and
                self.user.ClientToScreen(hwnd, self.c.byref(origin))):
            raise WindowProbeError("ocr_window_changed")
        return origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom

    def monitor_info(self, handle):
        info = self.monitor_type()
        info.cbSize = self.c.sizeof(info)
        if not self.user.GetMonitorInfoW(handle, self.c.byref(info)):
            raise WindowProbeError("ocr_dpi_unavailable")
        x, y = self.w.UINT(), self.w.UINT()
        if self.shcore.GetDpiForMonitor(handle, 0, self.c.byref(x), self.c.byref(y)) < 0 or not x.value:
            raise WindowProbeError("ocr_dpi_unavailable")
        bounds = lambda r: (r.left, r.top, r.right, r.bottom)
        return MonitorInfo(info.szDevice, bounds(info.rcMonitor), bounds(info.rcWork), x.value)

    def monitor_for_window(self, hwnd):
        handle = self.user.MonitorFromWindow(hwnd, 2)  # nearest monitor, even partly offscreen
        if not handle:
            raise WindowProbeError("ocr_dpi_unavailable")
        return self.monitor_info(handle)

    def monitors(self):
        handles = []
        @self.monitor_callback
        def collect(handle, _dc, _rect, _data):
            handles.append(handle)
            return True
        if not self.user.EnumDisplayMonitors(None, None, collect, 0):
            raise WindowProbeError("ocr_dpi_unavailable")
        return [self.monitor_info(handle) for handle in handles]

    def previous_window(self, hwnd):
        return self.user.GetWindow(hwnd, 3)  # GW_HWNDPREV, not a point-sampling heuristic

    def visible_bounds(self, hwnd):
        rect = self.w.RECT()
        if self.dwm.DwmGetWindowAttribute(hwnd, 9, self.c.byref(rect), self.c.sizeof(rect)) < 0:
            if not self.user.GetWindowRect(hwnd, self.c.byref(rect)):
                raise WindowProbeError("ocr_window_changed")
        return rect.left, rect.top, rect.right, rect.bottom

    def transparent(self, hwnd):
        color, alpha, flags = self.w.DWORD(), self.w.BYTE(), self.w.DWORD()
        success = self.user.GetLayeredWindowAttributes(hwnd, self.c.byref(color), self.c.byref(alpha), self.c.byref(flags))
        return bool(success and flags.value & 2 and alpha.value == 0)  # LWA_ALPHA

    def shape_intersects(self, hwnd, bounds):
        region = self.gdi.CreateRectRgn(0, 0, 0, 0)
        if not region:
            raise WindowProbeError("ocr_capture_unavailable")
        try:
            kind = self.user.GetWindowRgn(hwnd, region)
            if kind == 0:  # No explicit shape: conservatively use window rectangle.
                return True
            if kind == 1:  # NULLREGION
                return False
            rect = self.w.RECT()
            if not self.user.GetWindowRect(hwnd, self.c.byref(rect)):
                raise WindowProbeError("ocr_window_changed")
            if not self.gdi.OffsetRgn(region, rect.left, rect.top):
                raise WindowProbeError("ocr_capture_unavailable")
            return bool(self.gdi.RectInRegion(region, self.c.byref(self.w.RECT(*bounds))))
        finally:
            self.gdi.DeleteObject(region)

    def bring_forward(self, hwnd):
        if self.minimized(hwnd):
            self.user.ShowWindow(hwnd, 9)  # SW_RESTORE, caller initiated only
        self.user.SetForegroundWindow(hwnd)
        return self.user.GetAncestor(self.user.GetForegroundWindow(), 2) == hwnd


@lru_cache(maxsize=1)
def _api():
    if os.name != "nt":
        raise WindowProbeError("ocr_windows_only")
    return _Win32()


def enable_process_dpi_awareness() -> bool:
    """Call before constructing any Tk window; never change this at calibration."""
    if os.name != "nt":
        return False
    try:
        return _api().enable_process_dpi()
    except (AttributeError, OSError):
        return False


@contextmanager
def physical_pixels():
    api = _api()
    previous = api.set_thread_dpi(-4)
    if not previous:
        raise WindowProbeError("ocr_dpi_unavailable")
    try:
        yield
    finally:
        api.set_thread_dpi(previous)


def _candidates(api):
    candidates = []
    for hwnd in api.handles():
        if not api.visible(hwnd) or ntpath.basename(api.process_path(hwnd)).casefold() != "zoom.exe":
            continue
        if api.cloaked(hwnd):
            continue
        bounds = api.client_bounds(hwnd)
        minimized = api.minimized(hwnd)
        # Zoom creates visible 0x0/tiny utility windows. Reject them before
        # testing meeting hints or counting ambiguity, even with meeting titles.
        if not minimized and (bounds[2] - bounds[0] < 100 or bounds[3] - bounds[1] < 80):
            continue
        title, class_name = api.metadata(hwnd)
        rank = _meeting_rank(title, class_name)
        if not rank or (minimized and rank < 2):
            continue
        # A minimized main root is retained for a precise diagnostic/explicit
        # restore even if Windows reports an empty client rectangle for it.
        candidates.append(_Candidate(hwnd, title, class_name, bounds, rank, minimized))
    return candidates


def _select(api):
    candidates = _candidates(api)
    if not candidates:
        raise WindowProbeError("zoom_window_not_found")
    # Distinct main meeting roots are independent plausible meetings. Never
    # resolve those by size, foreground state, PID or enumeration order.
    main = [candidate for candidate in candidates if candidate.rank == 2]
    if main:
        if len(main) > 1:
            raise WindowProbeError("zoom_window_ambiguous")
        return main[0]
    # With only auxiliary video surfaces, use the largest valid client area.
    # Equal largest areas have no principled tie-break and remain ambiguous.
    largest = max(candidate.area for candidate in candidates)
    best = [candidate for candidate in candidates if candidate.area == largest]
    if len(best) != 1:
        raise WindowProbeError("zoom_window_ambiguous")
    return best[0]


def find_meeting_window() -> MeetingWindow:
    """Redetect the unique meeting every time; focus is irrelevant to validity."""
    with physical_pixels():
        api = _api()
        candidate = _select(api)
        hwnd = candidate.hwnd
        if candidate.minimized:
            raise WindowProbeError("ocr_window_minimized")
        bounds = api.client_bounds(hwnd)
        if bounds[2] - bounds[0] < 100 or bounds[3] - bounds[1] < 80:
            raise WindowProbeError("ocr_window_too_small")
        monitor = api.monitor_for_window(hwnd)
        return MeetingWindow(hwnd, api.pid(hwnd), candidate.title, candidate.class_name,
                             bounds, monitor.device, monitor.dpi)


def get_meeting_window(hwnd: int) -> MeetingWindow:
    """Revalidate a transient calibration HWND; multiple meetings stay ambiguous."""
    window = find_meeting_window()
    if window.hwnd != hwnd:
        raise WindowProbeError("ocr_window_changed")
    return window


def meeting_client_bounds() -> tuple[int, Bounds]:
    window = find_meeting_window()
    return window.hwnd, window.client_bounds


def list_monitors() -> list[MonitorInfo]:
    with physical_pixels():
        return _api().monitors()


def bring_meeting_to_front() -> MeetingWindow:
    """Explicit user action for calibration, never invoked by the capture loop."""
    with physical_pixels():
        api = _api()
        hwnd = _select(api).hwnd
        if not api.bring_forward(hwnd):
            raise WindowProbeError("ocr_focus_failed")
    return get_meeting_window(hwnd)


def ensure_region_uncovered(hwnd: int, region: Bounds) -> None:
    """Reject any actual intersecting window above Zoom, across the entire ROI.

    Invisible, minimized, DWM-cloaked and globally alpha-zero windows do not
    cover pixels. Explicit window regions are respected. Partly transparent
    windows remain occluders because their text can contaminate OCR. The check
    is necessarily a snapshot; callers repeat it immediately after capture.
    """
    with physical_pixels():
        api = _api()
        window = get_meeting_window(hwnd)
        if intersection(region, window.client_bounds) != region:
            raise WindowProbeError("ocr_window_changed")
        if not covered_by_monitors(region, [monitor.bounds for monitor in api.monitors()]):
            raise WindowProbeError("ocr_region_offscreen")
        above = api.previous_window(hwnd)
        visited = {hwnd}
        while above:
            if above in visited or len(visited) >= 4096:
                raise WindowProbeError("ocr_capture_unavailable")
            visited.add(above)
            if api.visible(above) and not api.minimized(above):
                overlap = intersection(region, api.visible_bounds(above))
                if (overlap is not None and not api.cloaked(above) and not api.transparent(above)
                        and api.shape_intersects(above, overlap)):
                    raise WindowProbeError("ocr_region_occluded")
            above = api.previous_window(above)

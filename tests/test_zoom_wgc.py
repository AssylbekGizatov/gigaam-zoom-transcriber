"""WGC lifecycle contracts plus a real covered-HWND pixel test on Windows."""
from datetime import timedelta
import os
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from gigaam_zoom import zoom_wgc as module
from gigaam_zoom.zoom_wgc import WgcCapture, WgcError


def _fake_api(monkeypatch, *, frames=True, border_denied=False):
    calls = []
    pool_holder = []

    class Resource:
        def __init__(self, kind):
            self.kind = kind

        def close(self):
            calls.append("close:" + self.kind)

    class Frame(Resource):
        def __init__(self, stamp=None):
            super().__init__("frame")
            self.content_size = SimpleNamespace(width=4, height=3)
            self.system_relative_time = timedelta(seconds=time.perf_counter() if stamp is None else stamp)
            self.surface = Resource("surface")

    class Session(Resource):
        def __init__(self):
            super().__init__("session")

        def __setattr__(self, name, value):
            if name == "is_border_required" and border_denied:
                raise PermissionError("Windows requires border")
            object.__setattr__(self, name, value)

        def start_capture(self):
            calls.append("start")

    class Pool(Resource):
        def __init__(self):
            super().__init__("pool")
            self.pending = []
            self.last_generated = 0

        @staticmethod
        def create_free_threaded(device, pixel_format, count, size):
            pool = Pool()
            pool_holder.append(pool)
            calls.append("create_pool")
            return pool

        def create_capture_session(self, item):
            return Session()

        def try_get_next_frame(self):
            if self.pending:
                return self.pending.pop(0)
            now = time.perf_counter()
            if frames and now - self.last_generated > .005:
                self.last_generated = now
                return Frame()
            return None

    class Buffer(bytearray):
        length = 0

    class Bitmap(Resource):
        pixel_width = 4
        pixel_height = 3

        def __init__(self, kind="bitmap"):
            super().__init__(kind)

        def copy_to_buffer(self, buffer):
            buffer[:] = bytes((20, 30, 210, 255)) * 12

    class SoftwareBitmap:
        @staticmethod
        async def create_copy_from_surface_async(surface):
            return Bitmap()

        @staticmethod
        def convert_with_alpha(bitmap, pixel_format, alpha):
            return Bitmap("converted")

    api = SimpleNamespace(
        init_apartment=lambda value: calls.append("init"), uninit_apartment=lambda: calls.append("uninit"),
        ApartmentType=SimpleNamespace(MULTI_THREADED=1),
        Session=SimpleNamespace(is_supported=lambda: True),
        wrap_device=lambda value: value,
        create_for_window=lambda hwnd: SimpleNamespace(size=SimpleNamespace(width=4, height=3)),
        FramePool=Pool, PixelFormat=SimpleNamespace(B8_G8_R8_A8_UINT_NORMALIZED=87),
        SoftwareBitmap=SoftwareBitmap, BitmapPixelFormat=SimpleNamespace(BGRA8=87),
        BitmapAlphaMode=SimpleNamespace(IGNORE=2), Buffer=Buffer)
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module, "_load_api", lambda: api)
    monkeypatch.setattr(module, "_create_d3d_device", lambda wrap: Resource("device"))
    return calls, pool_holder, Frame


def test_reuses_native_session_and_returns_detached_window_pixels(monkeypatch):
    calls, pools, _ = _fake_api(monkeypatch)
    capture = WgcCapture()
    first = capture.capture_window(100, .2)
    second = capture.capture_window(100, .2)
    capture.close()
    assert len(pools) == 1
    assert calls.count("init") == calls.count("uninit") == 1
    assert calls.index("close:session") < calls.index("close:pool") < calls.index("close:device") < calls.index("uninit")
    assert first.getpixel((2, 1)) == second.getpixel((2, 1)) == (210, 30, 20)
    assert first.info["capture_backend"] == "wgc"
    first.close()
    second.close()


def test_discards_buffered_frames_from_before_new_request(monkeypatch):
    calls, pools, frame_type = _fake_api(monkeypatch)
    capture = WgcCapture()
    with capture.capture_window(100, .2):
        pass
    pools[0].pending.append(frame_type(time.perf_counter() - 20))
    requested = time.perf_counter()
    requested_ms = time.monotonic() * 1000
    with capture.capture_window(100, .2) as result:
        assert result.info["wgc_system_relative_time_ms"] >= requested * 1000 - 1
        assert requested_ms - 1 <= result.info["captured_at_ms"] <= time.monotonic() * 1000 + 1
    capture.close()
    assert calls.count("close:frame") >= 3


def test_native_policy_denial_of_borderless_capture_does_not_disable_capture(monkeypatch):
    _fake_api(monkeypatch, border_denied=True)
    capture = WgcCapture()
    try:
        with capture.capture_window(100, .2) as image:
            assert image.size == (4, 3)
    finally:
        capture.close()


def test_switch_hwnd_releases_old_target_and_starts_new_session(monkeypatch):
    calls, pools, _ = _fake_api(monkeypatch)
    capture = WgcCapture()
    try:
        with capture.capture_window(100, .2):
            pass
        with capture.capture_window(200, .2):
            pass
        assert len(pools) == 2
        assert calls.count("close:session") == 1
    finally:
        capture.close()


def test_no_native_frames_times_out_and_releases_target(monkeypatch):
    calls, _, _ = _fake_api(monkeypatch, frames=False)
    capture = WgcCapture()
    started = time.monotonic()
    try:
        with pytest.raises(WgcError, match="wgc_timeout"):
            capture.capture_window(100, .03)
        assert time.monotonic() - started < .3
        assert "close:session" in calls and "close:pool" in calls
    finally:
        capture.close()
    assert calls[-1] == "uninit"


def test_failure_during_device_creation_balances_apartment(monkeypatch):
    calls, _, _ = _fake_api(monkeypatch)
    def fail(wrap):
        raise WgcError("wgc_device_unavailable")
    monkeypatch.setattr(module, "_create_d3d_device", fail)
    capture = WgcCapture()
    with pytest.raises(WgcError, match="wgc_device_unavailable"):
        capture.capture_window(100, .2)
    capture.close()  # Idempotent after failed initialization.
    assert calls == ["init", "uninit"]


def test_device_close_error_still_closes_loop_and_balances_com(monkeypatch):
    calls, _, _ = _fake_api(monkeypatch)
    capture = WgcCapture()
    with capture.capture_window(100, .2):
        pass
    def fail_close():
        raise OSError("native device was already removed")
    capture._device = SimpleNamespace(close=fail_close)
    loop = capture._loop
    capture.close()
    assert loop.is_closed()
    assert calls[-1] == "uninit"


def test_inconsistent_future_compositor_clock_is_rejected(monkeypatch):
    _, pools, frame_type = _fake_api(monkeypatch, frames=False)
    capture = WgcCapture()
    try:
        capture._initialize()
        capture._start_target(100)
        pools[0].pending.append(frame_type(time.perf_counter() + 100))
        with pytest.raises(WgcError, match="wgc_invalid_frame_time"):
            capture.capture_window(100, .2)
    finally:
        capture.close()


@pytest.fixture
def covered_capture_window():
    """Live red HWND covered by a blue HWND, shared by native capture tests."""
    if os.name != "nt":
        pytest.skip("Native HWND capture requires Windows")
    import ctypes as c
    from ctypes import wintypes as w
    import uuid

    user = c.WinDLL("user32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    gdi = c.WinDLL("gdi32", use_last_error=True)
    user.OpenInputDesktop.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    user.OpenInputDesktop.restype = w.HANDLE
    user.GetUserObjectInformationW.argtypes = [w.HANDLE, c.c_int, w.LPVOID, w.DWORD, c.POINTER(w.DWORD)]
    user.CloseDesktop.argtypes = [w.HANDLE]
    desktop = user.OpenInputDesktop(0, False, 1)
    if not desktop:
        pytest.skip(f"No accessible interactive input desktop (Win32 {c.get_last_error()})")
    try:
        name, needed = c.create_unicode_buffer(256), w.DWORD()
        if not user.GetUserObjectInformationW(desktop, 2, name, c.sizeof(name), c.byref(needed)):
            pytest.skip(f"Cannot inspect input desktop (Win32 {c.get_last_error()})")
        if name.value.casefold() != "default":
            pytest.skip("Input desktop is locked/noninteractive: " + name.value)
    finally:
        user.CloseDesktop(desktop)

    wndproc_type = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    class WindowClass(c.Structure):
        _fields_ = [("style", w.UINT), ("proc", wndproc_type), ("cls_extra", c.c_int),
                    ("wnd_extra", c.c_int), ("instance", w.HINSTANCE), ("icon", w.HICON),
                    ("cursor", w.HANDLE), ("background", w.HBRUSH),
                    ("menu", w.LPCWSTR), ("name", w.LPCWSTR)]

    kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
    kernel.GetModuleHandleW.restype = w.HMODULE
    user.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user.DefWindowProcW.restype = c.c_ssize_t
    user.RegisterClassW.argtypes = [c.POINTER(WindowClass)]
    user.RegisterClassW.restype = w.ATOM
    user.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                    c.c_int, c.c_int, c.c_int, c.c_int,
                                    w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
    user.CreateWindowExW.restype = w.HWND
    user.DestroyWindow.argtypes = [w.HWND]
    user.UnregisterClassW.argtypes = [w.LPCWSTR, w.HINSTANCE]
    user.ShowWindow.argtypes = [w.HWND, c.c_int]
    user.UpdateWindow.argtypes = [w.HWND]
    user.SetClassLongPtrW.argtypes = [w.HWND, c.c_int, c.c_void_p]
    user.SetClassLongPtrW.restype = c.c_void_p
    user.InvalidateRect.argtypes = [w.HWND, c.POINTER(w.RECT), w.BOOL]
    user.PeekMessageW.argtypes = [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
    user.DispatchMessageW.argtypes = [c.POINTER(w.MSG)]
    user.DispatchMessageW.restype = c.c_ssize_t
    user.TranslateMessage.argtypes = [c.POINTER(w.MSG)]
    gdi.CreateSolidBrush.argtypes = [w.DWORD]
    gdi.CreateSolidBrush.restype = w.HBRUSH
    gdi.DeleteObject.argtypes = [w.HGDIOBJ]
    ready, stop = queue.Queue(), threading.Event()

    def windows_thread():
        classes, brushes, windows = [], [], []
        instance = kernel.GetModuleHandleW(None)
        proc = wndproc_type(user.DefWindowProcW)
        try:
            for index, color in enumerate((0x00141ED2, 0x00D23C14)):
                name = "WgcPixelTest" + uuid.uuid4().hex
                brush = gdi.CreateSolidBrush(color)
                brushes.append(brush)
                definition = WindowClass(0, proc, 0, 0, instance, None, None, brush, None, name)
                if not user.RegisterClassW(c.byref(definition)):
                    raise OSError(c.get_last_error(), "RegisterClassW")
                classes.append(name)
                hwnd = user.CreateWindowExW(8 if index else 0, name, "WGC pixel test",
                                            0x90000000, 100, 100, 200, 140,
                                            None, None, instance, None)
                if not hwnd:
                    raise OSError(c.get_last_error(), "CreateWindowExW")
                windows.append(hwnd)
                user.ShowWindow(hwnd, 5)
                user.UpdateWindow(hwnd)
            ready.put(windows[0])
            msg = w.MSG()
            alternate = gdi.CreateSolidBrush(0x00141EDC)  # A second red shade.
            brushes.append(alternate)
            next_redraw, toggle = 0.0, False
            while not stop.wait(.005):
                if time.monotonic() >= next_redraw:
                    # WGC can throttle unchanged/covered surfaces. Actually
                    # change the target's pixels, not merely its window title,
                    # so a second fresh-frame read has a compositor update.
                    toggle = not toggle
                    user.SetClassLongPtrW(windows[0], -10, alternate if toggle else brushes[0])
                    user.InvalidateRect(windows[0], None, True)
                    user.UpdateWindow(windows[0])
                    next_redraw = time.monotonic() + .03
                while user.PeekMessageW(c.byref(msg), None, 0, 0, 1):
                    user.TranslateMessage(c.byref(msg))
                    user.DispatchMessageW(c.byref(msg))
        except Exception as error:
            ready.put(error)
        finally:
            for hwnd in reversed(windows):
                user.DestroyWindow(hwnd)
            for name in classes:
                user.UnregisterClassW(name, instance)
            for brush in brushes:
                gdi.DeleteObject(brush)

    thread = threading.Thread(target=windows_thread, daemon=True)
    thread.start()
    try:
        target = ready.get(timeout=5)
        if isinstance(target, Exception):
            raise target
        yield target
    finally:
        stop.set()
        thread.join(3)
    assert not thread.is_alive()


def test_native_wgc_reads_target_pixels_even_when_target_is_covered(covered_capture_window):
    """Actual D3D/WinRT path; a screen-capture substitute would return blue."""
    target = covered_capture_window
    capture = WgcCapture()
    try:
        try:
            image = capture.capture_window(target, timeout=3)
        except WgcError as error:
            if error.code == "wgc_unsupported":
                pytest.skip("GraphicsCaptureSession.is_supported() is false on this Windows host")
            raise
        with image:
            assert image.width >= 100 and image.height >= 80
            red, green, blue = image.getpixel((image.width // 2, image.height // 2))
            assert red > 170 and green < 80 and blue < 80, (red, green, blue)
        with capture.capture_window(target, timeout=3) as second:
            assert second.getpixel((second.width // 2, second.height // 2))[0] > 170
    finally:
        capture.close()

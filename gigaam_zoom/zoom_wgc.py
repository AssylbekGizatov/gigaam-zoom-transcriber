"""Windows.Graphics.Capture of one HWND, wholly local and memory-only.

This adapter owns a reusable free-threaded frame pool and D3D11 device. Use it
on one thread in the disposable native-capture helper. Poll/async timeouts here
bound normal waits; the owning helper must enforce a hard process timeout for
driver calls that do not return to Python. No desktop pixels, title matching,
screen scraping, temporary images or remote services are used by this backend.
"""
from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from types import SimpleNamespace


class WgcError(RuntimeError):
    def __init__(self, code: str, hresult: int | None = None):
        self.code = code
        self.hresult = hresult
        super().__init__(code)


def _load_api():
    try:
        from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
        from winrt.windows.graphics.capture import Direct3D11CaptureFramePool, GraphicsCaptureSession
        from winrt.windows.graphics.capture.interop import create_for_window
        from winrt.windows.graphics.directx import DirectXPixelFormat
        from winrt.windows.graphics.directx.direct3d11.interop import create_direct3d11_device_from_dxgi_device
        from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
        from winrt.windows.storage.streams import Buffer
    except ImportError as error:
        raise WgcError("wgc_dependency_missing") from error
    return SimpleNamespace(
        ApartmentType=ApartmentType, init_apartment=init_apartment, uninit_apartment=uninit_apartment,
        FramePool=Direct3D11CaptureFramePool, Session=GraphicsCaptureSession,
        create_for_window=create_for_window, PixelFormat=DirectXPixelFormat,
        wrap_device=create_direct3d11_device_from_dxgi_device,
        SoftwareBitmap=SoftwareBitmap, BitmapPixelFormat=BitmapPixelFormat,
        BitmapAlphaMode=BitmapAlphaMode, Buffer=Buffer)


def _create_d3d_device(wrap_device):
    """Create a BGRA D3D11 device, then wrap its IDXGIDevice through PyWinRT.

    ctypes is needed only for the documented D3D11CreateDevice/IUnknown ABI;
    all GraphicsCapture interop is supplied by the pinned PyWinRT wheels.
    """
    import ctypes as c
    import uuid

    class GUID(c.Structure):
        _fields_ = [("data1", c.c_uint32), ("data2", c.c_uint16),
                    ("data3", c.c_uint16), ("data4", c.c_ubyte * 8)]

    def method(pointer, slot, result, *arguments):
        table = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
        return c.WINFUNCTYPE(result, c.c_void_p, *arguments)(table[slot])

    def release(pointer):
        if pointer.value:
            method(pointer, 2, c.c_uint32)(pointer)
            pointer.value = None

    d3d = c.WinDLL("d3d11", use_last_error=True)
    create = d3d.D3D11CreateDevice
    create.argtypes = [c.c_void_p, c.c_uint32, c.c_void_p, c.c_uint32,
                       c.POINTER(c.c_uint32), c.c_uint32, c.c_uint32,
                       c.POINTER(c.c_void_p), c.POINTER(c.c_uint32), c.POINTER(c.c_void_p)]
    create.restype = c.c_int32
    iid_dxgi = GUID.from_buffer_copy(uuid.UUID("54ec77fa-1377-44e6-8c32-88fd5f44c84c").bytes_le)
    last_hresult = None
    for driver in (1, 5):  # D3D_DRIVER_TYPE_HARDWARE, then WARP software fallback.
        device, context, dxgi = c.c_void_p(), c.c_void_p(), c.c_void_p()
        try:
            last_hresult = create(None, driver, None, 0x20, None, 0, 7,
                                  c.byref(device), None, c.byref(context))
            if last_hresult < 0:
                continue
            query = method(device, 0, c.c_int32, c.POINTER(GUID), c.POINTER(c.c_void_p))
            last_hresult = query(device, c.byref(iid_dxgi), c.byref(dxgi))
            if last_hresult < 0:
                continue
            # The WinRT wrapper retains the DXGI device; release every local
            # native reference below, including the unused immediate context.
            return wrap_device(dxgi.value)
        finally:
            release(dxgi)
            release(context)
            release(device)
    raise WgcError("wgc_device_unavailable", last_hresult)


def _size(size) -> tuple[int, int]:
    return int(size.width), int(size.height)


def _check_size(size: tuple[int, int]) -> None:
    # Bound allocations for a malformed/destroyed capture target.
    if size[0] <= 0 or size[1] <= 0 or size[0] > 16384 or size[1] > 16384 or size[0] * size[1] > 40_000_000:
        raise WgcError("wgc_invalid_frame")


class WgcCapture:
    """Reusable HWND capture, owned and closed on the creating thread.

    Returned RGB PIL images are detached copies of the full HWND texture's
    content extent. The caller maps this extent to physical window/client
    bounds and must reject a geometry change instead of guessing the origin.
    """

    def __init__(self):
        self._thread = threading.get_ident()
        self._api = self._device = self._pool = self._session = self._item = None
        self._loop = None
        self._initialized = False
        self._hwnd = None
        self._pool_size = None
        self._closed = False

    def _check_thread(self):
        if threading.get_ident() != self._thread:
            raise WgcError("wgc_wrong_thread")

    def _initialize(self):
        if self._api is not None:
            return
        if os.name != "nt":
            raise WgcError("wgc_windows_only")
        api = _load_api()
        api.init_apartment(api.ApartmentType.MULTI_THREADED)
        self._initialized = True
        self._api = api
        try:
            if not api.Session.is_supported():
                raise WgcError("wgc_unsupported")
            self._device = _create_d3d_device(api.wrap_device)
            self._loop = asyncio.new_event_loop()
        except Exception:
            self.close()
            raise

    def _close_target(self):
        # Stop production before releasing the buffers and target references.
        for attribute in ("_session", "_pool"):
            resource = getattr(self, attribute)
            setattr(self, attribute, None)
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        self._item = None
        self._hwnd = None
        self._pool_size = None

    def close(self):
        self._check_thread()
        self._close_target()
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            finally:
                self._device = None
        if self._loop is not None:
            self._loop.close()
            self._loop = None
        if self._initialized:
            self._api.uninit_apartment()
            self._initialized = False
        self._api = None
        self._closed = True

    def _start_target(self, hwnd):
        self._close_target()
        api = self._api
        try:
            self._item = api.create_for_window(hwnd)
            size = _size(self._item.size)
            _check_size(size)
            self._pool = api.FramePool.create_free_threaded(
                self._device, api.PixelFormat.B8_G8_R8_A8_UINT_NORMALIZED, 2, self._item.size)
            self._session = self._pool.create_capture_session(self._item)
            # These properties may be unavailable/denied by Windows policy.
            # Do not request capabilities, show permission dialogs or try to
            # bypass a mandatory capture border. Windows owns the final choice.
            try:
                self._session.is_cursor_capture_enabled = False
            except Exception:
                pass
            try:
                self._session.is_border_required = False
            except Exception:
                pass
            self._session.start_capture()
            self._hwnd, self._pool_size = hwnd, size
        except Exception:
            self._close_target()
            raise

    async def _next_frame_image(self, deadline, requested_at):
        frame = None
        bitmap = converted = surface = None
        try:
            while frame is None:
                if time.monotonic() >= deadline:
                    raise WgcError("wgc_timeout")
                frame = self._pool.try_get_next_frame()
                if frame is not None and frame.system_relative_time.total_seconds() < requested_at:
                    frame.close()
                    frame = None
                if frame is None:
                    await asyncio.sleep(min(.01, max(0, deadline - time.monotonic())))
            # Drop queued old frames; process the newest compositor content.
            for _ in range(4):
                newer = self._pool.try_get_next_frame()
                if newer is None:
                    break
                frame.close()
                frame = newer
            content_size = _size(frame.content_size)
            _check_size(content_size)
            frame_seconds = frame.system_relative_time.total_seconds()
            if not math.isfinite(frame_seconds) or frame_seconds > time.perf_counter() + .01:
                raise WgcError("wgc_invalid_frame_time")
            if content_size != self._pool_size or _size(self._item.size) != self._pool_size:
                raise WgcError("wgc_window_changed")
            surface = frame.surface
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WgcError("wgc_timeout")
            api = self._api
            try:
                bitmap = await asyncio.wait_for(
                    api.SoftwareBitmap.create_copy_from_surface_async(surface), timeout=remaining)
            except TimeoutError as error:
                raise WgcError("wgc_timeout") from error
            converted = api.SoftwareBitmap.convert_with_alpha(
                bitmap, api.BitmapPixelFormat.BGRA8, api.BitmapAlphaMode.IGNORE)
            width, height = int(converted.pixel_width), int(converted.pixel_height)
            _check_size((width, height))
            if width < content_size[0] or height < content_size[1]:
                raise WgcError("wgc_invalid_frame")
            buffer = api.Buffer(width * height * 4)
            buffer.length = width * height * 4
            converted.copy_to_buffer(buffer)
            from PIL import Image
            full = Image.frombytes("RGB", (width, height), bytes(memoryview(buffer)), "raw", "BGRX")
            if full.size != content_size:
                result = full.crop((0, 0, *content_size))
                full.close()
            else:
                result = full
            result.info["capture_backend"] = "wgc"
            result.info["wgc_system_relative_time_ms"] = round(frame_seconds * 1000, 3)
            # Translate through frame age so attribution need not assume an
            # identical epoch for the capture QPC and audio monotonic clock.
            age_seconds = max(0.0, time.perf_counter() - frame_seconds)
            result.info["captured_at_ms"] = max(0, round(time.monotonic() * 1000 - age_seconds * 1000))
            if time.monotonic() > deadline:
                result.close()
                raise WgcError("wgc_timeout")
            return result
        finally:
            for resource in (converted, bitmap, surface, frame):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass

    def capture_window(self, hwnd: int, timeout: float = 1.0):
        self._check_thread()
        if self._closed:
            raise WgcError("wgc_closed")
        if type(hwnd) is not int or hwnd <= 0:
            raise ValueError("HWND must be a positive integer")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Capture timeout must be positive and finite")
        deadline = time.monotonic() + timeout
        # On Windows Python 3.13 perf_counter and WGC SystemRelativeTime both
        # use QueryPerformanceCounter's timebase. Never re-date a buffered
        # compositor frame from before this request as a fresh OCR observation.
        requested_at = time.perf_counter()
        try:
            self._initialize()
            if self._hwnd != hwnd or self._item is None or _size(self._item.size) != self._pool_size:
                self._start_target(hwnd)
            return self._loop.run_until_complete(self._next_frame_image(deadline, requested_at))
        except WgcError:
            self._close_target()
            raise
        except Exception as error:
            self._close_target()
            raise WgcError("wgc_capture_failed", getattr(error, "hresult", None)) from error


def capture_window(hwnd: int, timeout: float = 1.0):
    """One-shot convenience; repeated sampling should own a WgcCapture object."""
    capture = WgcCapture()
    try:
        return capture.capture_window(hwnd, timeout)
    finally:
        capture.close()

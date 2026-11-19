"""Window-first capture with bounded, reusable native isolation.

WGC and PrintWindow run in a persistent helper tied to the OCR process by a
Windows job. A blocked provider is killed before trying the next method. Only
screen-DC fallback requires uncovered pixels. Images never touch disk.
"""
from __future__ import annotations

from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from .zoom_window import WindowProbeError, MeetingWindow

METHODS = ("wgc", "printwindow", "screen_dc")
MAX_PNG_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 32 * 1024 * 1024


class CaptureError(RuntimeError):
    def __init__(self, code, *, method="", attempts=()):
        self.code, self.method, self.attempts = code, method, tuple(attempts)
        super().__init__(code)


@dataclass
class CapturedRegion:
    image: object
    method: str
    at_ms: int
    attempts: tuple[dict, ...]


def is_blank(image):
    from PIL import ImageStat
    grey = image.convert("L")
    try:
        low, high = grey.getextrema()
        return high - low < 4 or ImageStat.Stat(grey).stddev[0] < .8
    finally:
        grey.close()


def map_frame_to_client_crop(image_size, frame_bounds, client_bounds, crop):
    """Map screen physical pixels into a full-window texture without scaling."""
    from .zoom_window import intersection
    matches = {tuple(bounds) for bounds in frame_bounds
               if (bounds[2] - bounds[0], bounds[3] - bounds[1]) == tuple(image_size)
               and intersection(tuple(client_bounds), tuple(bounds)) == tuple(client_bounds)}
    if len(matches) != 1 or intersection(tuple(crop), tuple(client_bounds)) != tuple(crop):
        raise CaptureError("capture_geometry_mismatch")
    left, top, _, _ = matches.pop()
    return crop[0] - left, crop[1] - top, crop[2] - left, crop[3] - top


def _same_window(expected):
    from .zoom_window import find_meeting_window
    actual = find_meeting_window()
    if actual != expected:
        raise WindowProbeError("ocr_window_changed")
    return actual


class _KillOnCloseJob:
    """Do not leave native helpers alive when the outer OCR worker is killed."""
    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong),
                        ("flags", w.DWORD), ("min_working", c.c_size_t), ("max_working", c.c_size_t),
                        ("process_limit", w.DWORD), ("affinity", c.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class Io(c.Structure):
            _fields_ = [(name, c.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(c.Structure):
            _fields_ = [("basic", Basic), ("io", Io), ("process_memory", c.c_size_t),
                        ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.kernel.SetInformationJobObject.restype = w.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = w.BOOL
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not (self.handle and self.kernel.SetInformationJobObject(self.handle, 9, c.byref(info), c.sizeof(info))
                and self.kernel.AssignProcessToJobObject(self.handle, int(process._handle))):
            self.close()
            raise CaptureError("capture_job_unavailable")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _read_responses(stream, results):
    """A pipe read may block; the owner waits on a queue with a hard deadline."""
    try:
        while True:
            line = stream.readline(8193)
            if not line or len(line) > 8192:
                raise EOFError
            header = json.loads(line)
            size = header.get("bytes", 0)
            if type(size) is not int or not 0 <= size <= MAX_PNG_BYTES:
                raise ValueError
            data = bytearray()
            while len(data) < size:
                part = stream.read(size - len(data))
                if not part:
                    raise EOFError
                data.extend(part)
            results.put((header, bytes(data)))
    except (OSError, ValueError, EOFError, TypeError, AttributeError):
        results.put(({"error": "capture_worker_exited"}, b""))


class CaptureSession:
    """One helper/WGC session per OCR sampler, with failed-method backoff."""
    def __init__(self, *, native_timeout=2.5, retry_after=30.0, _command=None):
        self.native_timeout, self.retry_after = native_timeout, retry_after
        self._command = _command
        self._process = self._job = self._responses = self._reader = None
        self._retry_at = {}
        self._window_key = None

    def close(self):
        self._dispose(graceful=True)

    def _dispose(self, *, graceful=False):
        process, job, reader = self._process, self._job, self._reader
        self._process = self._job = self._responses = self._reader = None
        if process is not None and graceful and process.poll() is None:
            try:
                # EOF lets the helper close its frame pool/session/apartment.
                # A broken native cleanup is still bounded and then killed.
                process.stdin.close()
                process.wait(timeout=.5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if job is not None:
            job.close()  # On Windows this closes/kills the helper as one unit.
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass  # The job may have just terminated it.
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass  # Never let native teardown prevent the next fallback.
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if reader is not None:
            reader.join(timeout=.2)

    def _start(self):
        if self._process is not None and self._process.poll() is None:
            return
        self._dispose()
        command = self._command or [sys.executable, "-m", "gigaam_zoom.zoom_capture"]
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   bufsize=0, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._process = process
        try:
            self._job = _KillOnCloseJob(process)
        except Exception:
            self._dispose()
            raise
        self._responses = queue.Queue()
        self._reader = threading.Thread(target=_read_responses, args=(process.stdout, self._responses),
                                        name="window-capture-pipe", daemon=True)
        self._reader.start()

    def _native_capture(self, method, window, crop):
        from PIL import Image
        self._start()
        request = {"method": method, "hwnd": window.hwnd, "pid": window.pid,
                   "client_bounds": window.client_bounds, "crop": crop}
        try:
            self._process.stdin.write((json.dumps(request) + "\n").encode("ascii"))
            self._process.stdin.flush()
            header, data = self._responses.get(timeout=self.native_timeout)
        except queue.Empty:
            self._dispose()
            raise CaptureError("capture_" + method + "_timeout") from None
        except (OSError, BrokenPipeError):
            self._dispose()
            raise CaptureError("capture_worker_exited") from None
        if header.get("error"):
            if header["error"] == "capture_worker_exited":
                self._dispose()
            raise CaptureError(header["error"])
        if type(header.get("at_ms")) is not int:
            raise CaptureError("capture_protocol_error")
        try:
            with Image.open(io.BytesIO(data)) as encoded:
                if encoded.width * encoded.height > MAX_PIXELS:
                    raise CaptureError("capture_protocol_error")
                image = encoded.convert("RGB")
        except (OSError, ValueError) as error:
            raise CaptureError("capture_protocol_error") from error
        if image.size != (crop[2] - crop[0], crop[3] - crop[1]):
            image.close()
            raise CaptureError("capture_geometry_mismatch")
        return image, header["at_ms"]

    def _screen_capture(self, window, crop):
        from PIL import ImageGrab
        from .zoom_window import ensure_region_uncovered
        ensure_region_uncovered(window.hwnd, crop)
        at_ms = time.monotonic_ns() // 1_000_000
        image = ImageGrab.grab(bbox=crop, all_screens=True, include_layered_windows=True)
        try:
            _same_window(window)
            ensure_region_uncovered(window.hwnd, crop)
        except Exception:
            image.close()
            raise
        return image, at_ms

    def capture(self, window: MeetingWindow, crop):
        key = (window.hwnd, window.pid)
        if key != self._window_key:
            self._retry_at.clear()
            self._dispose(graceful=True)
            self._window_key = key
        attempts = []
        last_code = "ocr_capture_unavailable"
        for method in METHODS:
            if time.monotonic() < self._retry_at.get(method, 0):
                attempts.append({"method": method, "evidence": "capture_backoff"})
                continue
            image = None
            try:
                _same_window(window)
                if method == "screen_dc":
                    image, at_ms = self._screen_capture(window, crop)
                else:
                    image, at_ms = self._native_capture(method, window, crop)
                _same_window(window)
                if is_blank(image):
                    raise CaptureError("ocr_capture_blank")
                attempts.append({"method": method, "evidence": "capture_ok"})
                return CapturedRegion(image, method, at_ms, tuple(attempts))
            except WindowProbeError as error:
                attempts.append({"method": method, "evidence": error.code})
                raise CaptureError(error.code, method=method, attempts=attempts) from error
            except (CaptureError, OSError, ValueError) as error:
                last_code = error.code if isinstance(error, CaptureError) else "ocr_capture_unavailable"
                attempts.append({"method": method, "evidence": last_code})
                if last_code in {"ocr_window_changed", "ocr_window_minimized", "zoom_window_not_found",
                                 "zoom_window_ambiguous"}:
                    raise CaptureError(last_code, method=method, attempts=attempts) from error
                if method != "screen_dc":
                    # A temporarily empty nameplate can return as soon as the
                    # next speaker appears, even while Zoom is still covered.
                    delay = min(2.0, self.retry_after) if last_code == "ocr_capture_blank" else self.retry_after
                    self._retry_at[method] = time.monotonic() + delay
            finally:
                # Ownership transfers only on success.
                if image is not None and (not attempts or attempts[-1]["evidence"] != "capture_ok"):
                    image.close()
        raise CaptureError(last_code, method="screen_dc", attempts=attempts)


def _print_window(hwnd):
    """Real PrintWindow(PW_RENDERFULLCONTENT), never a mislabeled screen grab."""
    import ctypes as c
    from ctypes import wintypes as w
    from PIL import Image
    from .zoom_window import window_frame_bounds
    bounds = window_frame_bounds(hwnd)[0]  # GetWindowRect, including nonclient frame.
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise CaptureError("capture_geometry_mismatch")
    user, gdi = c.WinDLL("user32", use_last_error=True), c.WinDLL("gdi32", use_last_error=True)
    class BitmapHeader(c.Structure):
        _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                    ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD),
                    ("image_size", w.DWORD), ("xppm", w.LONG), ("yppm", w.LONG),
                    ("used", w.DWORD), ("important", w.DWORD)]
    class BitmapInfo(c.Structure):
        _fields_ = [("header", BitmapHeader), ("color", w.DWORD * 1)]
    def bind(dll, name, result, *arguments):
        function = getattr(dll, name)
        function.restype, function.argtypes = result, list(arguments)
    bind(user, "GetWindowDC", w.HDC, w.HWND)
    bind(user, "ReleaseDC", c.c_int, w.HWND, w.HDC)
    bind(user, "PrintWindow", w.BOOL, w.HWND, w.HDC, w.UINT)
    bind(gdi, "CreateCompatibleDC", w.HDC, w.HDC)
    bind(gdi, "CreateDIBSection", w.HANDLE, w.HDC, c.POINTER(BitmapInfo), w.UINT,
         c.POINTER(c.c_void_p), w.HANDLE, w.DWORD)
    bind(gdi, "SelectObject", w.HANDLE, w.HDC, w.HANDLE)
    bind(gdi, "DeleteObject", w.BOOL, w.HANDLE)
    bind(gdi, "DeleteDC", w.BOOL, w.HDC)
    source = user.GetWindowDC(hwnd)
    memory = bitmap = previous = None
    try:
        if not source:
            raise CaptureError("capture_printwindow_failed")
        memory = gdi.CreateCompatibleDC(source)
        info = BitmapInfo()
        info.header = BitmapHeader(c.sizeof(BitmapHeader), width, -height, 1, 32, 0,
                                   width * height * 4, 0, 0, 0, 0)
        pixels = c.c_void_p()
        bitmap = gdi.CreateDIBSection(source, c.byref(info), 0, c.byref(pixels), None, 0)
        if not memory or not bitmap or not pixels:
            raise CaptureError("capture_printwindow_failed")
        previous = gdi.SelectObject(memory, bitmap)
        if not previous or previous == c.c_void_p(-1).value:
            raise CaptureError("capture_printwindow_failed")
        c.memset(pixels, 0, width * height * 4)
        if not user.PrintWindow(hwnd, memory, 0x00000002):  # PW_RENDERFULLCONTENT
            raise CaptureError("capture_printwindow_failed")
        return Image.frombytes("RGB", (width, height), c.string_at(pixels, width * height * 4), "raw", "BGRX")
    finally:
        if previous and previous != c.c_void_p(-1).value and memory:
            gdi.SelectObject(memory, previous)
        if bitmap:
            gdi.DeleteObject(bitmap)
        if memory:
            gdi.DeleteDC(memory)
        if source:
            user.ReleaseDC(hwnd, source)


def _serve():
    from .zoom_window import enable_process_dpi_awareness, physical_pixels, find_meeting_window, window_frame_bounds
    enable_process_dpi_awareness()
    wgc = None
    try:
        for raw in sys.stdin.buffer:
            image = cropped = None
            method = None
            try:
                request = json.loads(raw)
                method = request["method"]
                if method not in {"wgc", "printwindow"}:
                    raise CaptureError("capture_protocol_error")
                with physical_pixels():
                    window = find_meeting_window()
                    if (window.hwnd != request["hwnd"] or window.pid != request["pid"]
                            or window.client_bounds != tuple(request["client_bounds"])):
                        raise CaptureError("ocr_window_changed")
                    bounds = window_frame_bounds(window.hwnd)
                    at_ms = time.monotonic_ns() // 1_000_000
                    if method == "wgc":
                        if wgc is None:
                            from .zoom_wgc import WgcCapture
                            wgc = WgcCapture()
                        image = wgc.capture_window(window.hwnd, timeout=1.0)
                        at_ms = image.info.get("captured_at_ms", at_ms)
                    else:
                        image = _print_window(window.hwnd)
                    _same_window(window)
                    if window_frame_bounds(window.hwnd) != bounds:
                        raise CaptureError("ocr_window_changed")
                    texture_crop = map_frame_to_client_crop(image.size, bounds, window.client_bounds, request["crop"])
                    cropped = image.crop(texture_crop)
                    output = io.BytesIO()
                    cropped.save(output, format="PNG")
                    data = output.getvalue()
                    if len(data) > MAX_PNG_BYTES:
                        raise CaptureError("capture_protocol_error")
                    header = {"at_ms": at_ms, "bytes": len(data)}
            except WindowProbeError as error:
                header, data = {"error": error.code}, b""
            except Exception as error:
                if method == "wgc" and wgc is not None:
                    wgc.close()
                    wgc = None
                code = getattr(error, "code", "capture_native_error")
                # Only fixed provider codes leave the child, never exception text.
                if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 80:
                    code = "capture_native_error"
                header, data = {"error": code}, b""
            finally:
                if cropped is not None:
                    cropped.close()
                if image is not None:
                    image.close()
            sys.stdout.buffer.write(json.dumps(header).encode("ascii") + b"\n" + data)
            sys.stdout.buffer.flush()
    finally:
        if wgc is not None:
            wgc.close()


if __name__ == "__main__":
    _serve()

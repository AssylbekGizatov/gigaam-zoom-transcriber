"""Local OCR of a user-selected Zoom Speaker View nameplate.

The user must select the *active speaker* nameplate with pin/spotlight disabled.
OCR cannot verify that layout or distinguish people with identical names. These
are display-name hints, never participant IDs or voice-derived identities.
Windows OCR uses installed language resources; screenshots stay in memory.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import math
import os
import unicodedata

from .zoom_accessibility import SpeakerObservation, monotonic_ms


UNKNOWN_SOURCE_ID = "local:zoom-ocr:unknown"


class VisualProbeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VisualSpeakerConfig:
    # Fractions of the Zoom client area, excluding title bar and borders.
    region: tuple[float, float, float, float]
    language: str = "ru-RU"
    client_size: tuple[int, int] | None = None

    def validate(self) -> None:
        if (not isinstance(self.region, (tuple, list)) or len(self.region) != 4
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(v) for v in self.region)):
            raise ValueError("Область имени: нужны четыре числа x, y, ширина, высота.")
        x, y, width, height = self.region
        if not (0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 and 0 < height <= 1
                and x + width <= 1.000001 and y + height <= 1.000001):
            raise ValueError("Область имени должна находиться внутри окна Zoom.")
        if (not isinstance(self.language, str) or not self.language.strip()
                or len(self.language) > 40):
            raise ValueError("Укажите язык Windows OCR, например ru-RU или en-US.")
        if self.client_size is not None and (
                not isinstance(self.client_size, (tuple, list))
                or len(self.client_size) != 2 or any(type(v) is not int or v <= 0
                                                for v in self.client_size)):
            raise ValueError("Некорректный размер окна при выборе области имени.")


def _unknown(at_ms: int, reason: str) -> SpeakerObservation:
    return SpeakerObservation(at_ms, UNKNOWN_SOURCE_ID, None, reason)


def parse_ocr_name(lines: list[str]) -> str | None:
    """A single complete line, not a guessed name from a screenful of text."""
    if len(lines) != 1 or not isinstance(lines[0], str):
        return None
    text = lines[0]
    if any(unicodedata.category(char).startswith("C") for char in text):
        return None
    name = unicodedata.normalize("NFC", text).strip()
    if not 1 <= len(name) <= 160 or "…" in name or "..." in name:
        return None
    if not any(char.isalnum() for char in name):
        return None
    # These indicate a wrong crop/layout; never turn buttons into identities.
    if name.casefold() in {
        "zoom", "zoom meeting", "zoom workplace", "speaker view", "gallery view",
        "participants", "mute", "unmute", "share", "share screen", "stop video",
        "start video", "record", "reactions", "more", "view", "chat", "leave",
        "вид докладчика", "вид галереи", "участники", "выключить звук",
        "включить звук", "демонстрация экрана", "чат", "выйти", "ещё", "еще",
    }:
        return None
    return name


class NameStabilizer:
    """Require two fresh equal readings; a gap/error clears the prior name."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._previous: tuple[str, int] | None = None

    def reset(self) -> None:
        self._previous = None

    def observe(self, lines: list[str], at_ms: int) -> SpeakerObservation:
        name = parse_ocr_name(lines)
        previous = self._previous
        if name is None:
            self.reset()
            return _unknown(at_ms, "ocr_name_missing")
        self._previous = (name, at_ms)
        if (previous is None or previous[0] != name
                or not 0 < at_ms - previous[1] <= 2000):
            return _unknown(at_ms, "ocr_confirming")
        digest = hashlib.sha256((self.session_id + "\0" + name).encode("utf-8")).hexdigest()[:20]
        return SpeakerObservation(at_ms, "local:zoom-ocr:" + digest, name, "ocr_speaker_view_hint")


@contextmanager
def _physical_pixels():
    """Keep geometry and Pillow capture in one per-monitor DPI coordinate space."""
    if os.name != "nt":
        raise VisualProbeError("ocr_windows_only")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    user32.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    if not previous:
        raise VisualProbeError("ocr_dpi_unavailable")
    try:
        yield
    finally:
        user32.SetThreadDpiAwarenessContext(previous)


def meeting_client_bounds() -> tuple[int, tuple[int, int, int, int]]:
    """Return one foreground meeting's physical client bounds, fail closed."""
    import ctypes
    from ctypes import wintypes
    from .zoom_accessibility import _meeting_windows

    with _physical_pixels():
        windows = _meeting_windows()
        if len(windows) != 1:
            raise VisualProbeError("ocr_window_missing" if not windows else "ocr_window_ambiguous")
        hwnd = windows[0][0]
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        user32.ClientToScreen.restype = wintypes.BOOL
        if user32.IsIconic(hwnd):
            raise VisualProbeError("ocr_window_minimized")
        if user32.GetAncestor(user32.GetForegroundWindow(), 2) != hwnd:  # GA_ROOT
            raise VisualProbeError("ocr_window_not_foreground")
        rect = wintypes.RECT()
        origin = wintypes.POINT(0, 0)
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)) or not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
            raise VisualProbeError("ocr_capture_unavailable")
        if rect.right < 100 or rect.bottom < 80:
            raise VisualProbeError("ocr_window_too_small")
        return hwnd, (origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom)


def region_bounds(bounds: tuple[int, int, int, int], config: VisualSpeakerConfig) -> tuple[int, int, int, int]:
    config.validate()
    left, top, right, bottom = bounds
    width, height = right - left, bottom - top
    if config.client_size is not None and (width, height) != tuple(config.client_size):
        raise VisualProbeError("ocr_recalibration_required")
    x, y, w, h = config.region
    crop = (left + round(x * width), top + round(y * height),
            min(right, left + round((x + w) * width)), min(bottom, top + round((y + h) * height)))
    if crop[2] - crop[0] < 16 or crop[3] - crop[1] < 8:
        raise VisualProbeError("ocr_region_too_small")
    return crop


def _check_uncovered(hwnd: int, bounds: tuple[int, int, int, int]) -> None:
    """Reject foreground overlays covering sampled points in the nameplate."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    left, top, right, bottom = bounds
    for x in (left + 1, (left + right) // 2, right - 2):
        for y in (top + 1, (top + bottom) // 2, bottom - 2):
            window = user32.WindowFromPoint(wintypes.POINT(x, y))
            if user32.GetAncestor(window, 2) != hwnd:
                raise VisualProbeError("ocr_region_occluded")


def _capture(config: VisualSpeakerConfig | None = None):
    try:
        from PIL import ImageGrab
    except ImportError as error:
        raise VisualProbeError("ocr_dependency_missing") from error
    with _physical_pixels():
        hwnd, bounds = meeting_client_bounds()
        crop = bounds if config is None else region_bounds(bounds, config)
        _check_uncovered(hwnd, crop)
        at_ms = monotonic_ms()  # Capture time, never OCR completion time.
        image = ImageGrab.grab(bbox=crop, all_screens=True)
        try:
            if meeting_client_bounds() != (hwnd, bounds):
                raise VisualProbeError("ocr_window_changed")
            _check_uncovered(hwnd, crop)
        except Exception:
            image.close()
            raise
        return image, bounds, at_ms


def capture_meeting_client():
    """Calibration screenshot and physical bbox; caller owns the in-memory image."""
    image, bounds, _ = _capture()
    return image, bounds


class WindowsOcr:
    """Native Windows.Media.Ocr, no executable, network or temporary image files."""

    def __init__(self, language: str):
        self._uninit = None
        try:
            from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
            from winrt.windows.globalization import Language
            from winrt.windows.media.ocr import OcrEngine
        except ImportError as error:
            raise VisualProbeError("ocr_dependency_missing") from error
        init_apartment(ApartmentType.MULTI_THREADED)
        self._uninit = uninit_apartment
        try:
            self.engine = OcrEngine.try_create_from_language(Language(language))
            if self.engine is None:
                raise VisualProbeError("ocr_language_unavailable")
            self.max_dimension = OcrEngine.max_image_dimension
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        # Called on the creating worker thread, paired even on initialization failure.
        self.engine = None
        if self._uninit is not None:
            self._uninit()
            self._uninit = None

    async def recognize(self, image) -> list[str]:
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter

        # Enlarge small nameplates while respecting Windows OCR's size limit.
        scale = min(3.0, self.max_dimension / max(image.size))
        resized = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))
        rgba = resized.convert("RGBA")
        writer = DataWriter()
        bitmap = None
        try:
            writer.write_bytes(rgba.tobytes("raw", "BGRA"))
            bitmap = SoftwareBitmap.create_copy_with_alpha_from_buffer(
                writer.detach_buffer(), BitmapPixelFormat.BGRA8, rgba.width, rgba.height,
                BitmapAlphaMode.IGNORE)
            result = await self.engine.recognize_async(bitmap)
            # Cropped words near an edge may be partial without an ellipsis.
            for line in result.lines:
                for word in line.words:
                    box = word.bounding_rect
                    if (box.x <= 1 or box.y <= 1 or box.x + box.width >= rgba.width - 1
                            or box.y + box.height >= rgba.height - 1):
                        raise VisualProbeError("ocr_name_clipped")
            return [line.text for line in result.lines if line.text.strip()]
        finally:
            if bitmap is not None:
                bitmap.close()
            writer.close()
            rgba.close()
            resized.close()


def _visual_worker(connection, session_id: str, config: VisualSpeakerConfig) -> None:
    """Killable process worker. OCR hangs are bounded by ZoomSpeakerObserver."""
    stabilizer = NameStabilizer(session_id)
    reader = None
    loop = asyncio.new_event_loop()
    try:
        config.validate()
        while connection.recv() == "sample":
            image = None
            try:
                if os.name != "nt":
                    raise VisualProbeError("ocr_windows_only")
                if reader is None:
                    reader = WindowsOcr(config.language)
                image, _, at_ms = _capture(config)
                lines = loop.run_until_complete(reader.recognize(image))
                if monotonic_ms() - at_ms > 2000:
                    raise VisualProbeError("ocr_stale")
                result = stabilizer.observe(lines, at_ms)
            except VisualProbeError as error:
                stabilizer.reset()
                result = _unknown(monotonic_ms(), error.code)
            except Exception:
                stabilizer.reset()
                result = _unknown(monotonic_ms(), "ocr_unavailable")
            finally:
                if image is not None:
                    image.close()
            connection.send(result)
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        if reader is not None:
            reader.close()
        loop.close()
        connection.close()

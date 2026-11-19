"""Local OCR of a user-selected Zoom Speaker View nameplate.

The user must select the *active speaker* nameplate with pin/spotlight disabled.
OCR cannot verify that layout or distinguish people with identical names. These
are display-name hints, never participant IDs or voice-derived identities.
Windows OCR uses installed language resources; screenshots stay in memory.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, asdict
import hashlib
import io
import math
import os
import re
import time
import unicodedata

from .speaker_observer import SpeakerObservation, monotonic_ms


UNKNOWN_SOURCE_ID = "local:zoom-ocr:unknown"
ASPECT_RATIO_TOLERANCE = .12


class VisualProbeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VisualSpeakerConfig:
    # Fractions of the Zoom client area, excluding title bar and borders.
    region: tuple[float, float, float, float]
    language: str = "auto"
    client_size: tuple[int, int] | None = None
    include_image: bool = False
    dump_ocr_text: bool = False

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
        if type(self.include_image) is not bool or type(self.dump_ocr_text) is not bool:
            raise ValueError("Некорректные параметры диагностики OCR.")


@dataclass(frozen=True, slots=True)
class VisualFrame:
    observation: SpeakerObservation
    raw_lines: tuple[str, ...] = ()
    parser_reason: str = "not_run"
    timings: dict[str, float] = field(default_factory=dict)
    window: dict = field(default_factory=dict)
    crop_bounds: tuple[int, int, int, int] | None = None
    image_size: tuple[int, int] | None = None
    backend: str = "windows_media_ocr"
    language: str = ""
    max_age_ms: int = 2000
    line_count: int = 0
    word_count: int = 0
    image_png: bytes | None = None
    error_info: dict | None = None
    capture_method: str = ""
    capture_attempts: tuple[dict, ...] = ()

    def diagnostic_dict(self) -> dict:
        result = asdict(self)
        result.pop("image_png", None)
        result["evidence"] = self.observation.evidence
        result["name"] = self.observation.display_name
        result["observed_at_ms"] = self.observation.observed_at_ms
        return result


@dataclass(frozen=True, slots=True)
class OcrReading:
    lines: tuple[str, ...]
    word_count: int
    clipped: bool = False


_UI_WORDS = {
    "zoom", "zoom meeting", "zoom workplace", "speaker view", "gallery view",
    "participants", "mute", "unmute", "share", "share screen", "stop video",
    "start video", "record", "recording", "pause recording", "stop recording",
    "reactions", "more", "view", "chat", "leave",
    "host", "co-host", "me", "you", "вид докладчика", "вид галереи", "участники",
    "выключить звук", "включить звук", "демонстрация экрана", "чат", "выйти",
    "ещё", "еще", "организатор", "соорганизатор", "я", "вы", "запись",
    "остановить запись", "приостановить запись", "идет запись", "идёт запись",
}
_UI_PATTERN = "|".join(re.escape(word) for word in sorted(_UI_WORDS, key=len, reverse=True))


def parse_ocr_decision(lines: list[str] | tuple[str, ...]) -> tuple[str | None, str]:
    """Remove exact UI labels/edge badges, then require one complete name line."""
    candidates = []
    for text in lines:
        if not isinstance(text, str) or any(unicodedata.category(c).startswith("C") for c in text):
            return None, "ocr_name_control_characters"
        name = unicodedata.normalize("NFC", text).strip()
        if not name:
            continue
        # Parenthesized roles and exact commands at an edge are Zoom chrome.
        name = re.sub(r"\s*[([](?:" + _UI_PATTERN + r")[)\]]\s*", " ", name, flags=re.I).strip()
        for _ in range(3):
            previous = name
            name = re.sub(r"^(?:" + _UI_PATTERN + r")(?:\s*:\s*|\s+|$)", "", name, flags=re.I).strip()
            name = re.sub(r"(?:\s+|^)(?:" + _UI_PATTERN + r")$", "", name, flags=re.I).strip()
            if name == previous:
                break
        if not name:
            continue
        if "…" in name or "..." in name:
            return None, "ocr_name_clipped"
        if len(name) > 160:
            return None, "ocr_name_too_long"
        if not any(char.isalpha() for char in name):
            return None, "ocr_name_no_letters"
        candidates.append(name)
    if not candidates:
        return None, "ocr_name_ui_only" if lines else "ocr_name_missing"
    if len(candidates) != 1:
        return None, "ocr_name_ambiguous"
    return candidates[0], "accepted"


def _unknown(at_ms: int, reason: str) -> SpeakerObservation:
    return SpeakerObservation(at_ms, UNKNOWN_SOURCE_ID, None, reason)


def parse_ocr_name(lines: list[str]) -> str | None:
    return parse_ocr_decision(lines)[0]


class NameStabilizer:
    """Require two fresh equal readings; a gap/error clears the prior name."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._previous: tuple[str, int] | None = None

    def reset(self) -> None:
        self._previous = None

    def observe(self, lines: list[str], at_ms: int, *, max_gap_ms: int = 2000) -> SpeakerObservation:
        name, reason = parse_ocr_decision(lines)
        previous = self._previous
        if name is None:
            self.reset()
            return _unknown(at_ms, reason)
        self._previous = (name, at_ms)
        if (previous is None or previous[0] != name
                or not 0 < at_ms - previous[1] <= max_gap_ms):
            return SpeakerObservation(at_ms, UNKNOWN_SOURCE_ID, None, "ocr_confirming", recognized_name=name)
        digest = hashlib.sha256((self.session_id + "\0" + name).encode("utf-8")).hexdigest()[:20]
        return SpeakerObservation(at_ms, "local:zoom-ocr:" + digest, name, "ocr_speaker_view_hint")


def region_bounds(bounds: tuple[int, int, int, int], config: VisualSpeakerConfig) -> tuple[int, int, int, int]:
    config.validate()
    left, top, right, bottom = bounds
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise VisualProbeError("ocr_capture_unavailable")
    if config.client_size is not None:
        baseline_ratio = config.client_size[0] / config.client_size[1]
        if abs((width / height) / baseline_ratio - 1) > ASPECT_RATIO_TOLERANCE:
            raise VisualProbeError("ocr_recalibration_required")
    x, y, w, h = config.region
    crop = (left + round(x * width), top + round(y * height),
            min(right, left + round((x + w) * width)), min(bottom, top + round((y + h) * height)))
    if crop[2] - crop[0] < 16 or crop[3] - crop[1] < 8:
        raise VisualProbeError("ocr_region_too_small")
    return crop


def _capture(config: VisualSpeakerConfig | None = None, *, session=None):
    from .zoom_window import WindowProbeError, physical_pixels, find_meeting_window
    from .zoom_capture import CaptureSession, CaptureError
    own_session = session is None
    session = session or CaptureSession()
    try:
        with physical_pixels():
            window = find_meeting_window()
            bounds = window.client_bounds
            crop = bounds if config is None else region_bounds(bounds, config)
            result = session.capture(window, crop)
            result.image.info["capture_method"] = result.method
            result.image.info["capture_attempts"] = result.attempts
            return result.image, window, crop, result.at_ms
    except (WindowProbeError, CaptureError) as error:
        translated = VisualProbeError(error.code)
        translated.capture_method = getattr(error, "method", "")
        translated.capture_attempts = getattr(error, "attempts", ())
        raise translated from error
    finally:
        if own_session:
            session.close()


def preferred_ocr_language(tags: list[str]) -> str:
    for preferred in ("ru", "ru-RU", "en-US"):
        match = next((tag for tag in tags if tag.casefold() == preferred.casefold()), None)
        if match:
            return match
    return tags[0] if tags else ""


def installed_ocr_languages() -> list[str]:
    try:
        from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
        from winrt.windows.media.ocr import OcrEngine
    except ImportError as error:
        raise VisualProbeError("ocr_dependency_missing") from error
    init_apartment(ApartmentType.MULTI_THREADED)
    try:
        return [language.language_tag for language in OcrEngine.available_recognizer_languages]
    finally:
        uninit_apartment()


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
            if language.casefold() == "auto":
                language = preferred_ocr_language([
                    item.language_tag for item in OcrEngine.available_recognizer_languages])
            if not language:
                raise VisualProbeError("ocr_language_unavailable")
            self.language = language
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

    async def recognize_details(self, image) -> OcrReading:
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
            # Preserve every raw line before any parser or clipping rejection.
            lines = tuple(line.text for line in result.lines)
            word_count = sum(len(line.words) for line in result.lines)
            clipped = False
            for line in result.lines:
                for word in line.words:
                    box = word.bounding_rect
                    if (box.x <= 1 or box.y <= 1 or box.x + box.width >= rgba.width - 1
                            or box.y + box.height >= rgba.height - 1):
                        clipped = True
            return OcrReading(lines, word_count, clipped)
        finally:
            if bitmap is not None:
                bitmap.close()
            writer.close()
            rgba.close()
            resized.close()

    async def recognize(self, image) -> list[str]:
        """Compatibility for native smoke tests; sampling uses unfiltered details."""
        reading = await self.recognize_details(image)
        if reading.clipped:
            raise VisualProbeError("ocr_name_clipped")
        return list(reading.lines)


def adaptive_max_age_ms(latency_ms: float) -> int:
    """Bounded freshness budget; never alter the original capture timestamp."""
    if not math.isfinite(latency_ms) or latency_ms < 0:
        return 2000
    return min(10000, max(2000, round(2 * latency_ms + 1500)))


def _blank_image(image) -> bool:
    from PIL import ImageStat
    grey = image.convert("L")
    try:
        low, high = grey.getextrema()
        return high - low < 4 or ImageStat.Stat(grey).stddev[0] < .8
    finally:
        grey.close()


class VisualSampler:
    """One shared backend for live attribution, preview and diagnostic CLI.

    Own this object on one worker thread/process. Calls may enter native code;
    GUI callers must use the killable ZoomSpeakerObserver process.
    """

    def __init__(self, config: VisualSpeakerConfig, session_id: str):
        from .zoom_capture import CaptureSession
        config.validate()
        self.config = config
        self.stabilizer = NameStabilizer(session_id)
        self.reader = None
        self.capture_session = CaptureSession()
        self.loop = asyncio.new_event_loop()
        self.latency_ms = 0.0
        self._geometry = None
        self._baseline_ratio = (config.client_size[0] / config.client_size[1]
                                if config.client_size is not None else None)

    def close(self) -> None:
        self.capture_session.close()
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        self.loop.close()

    def sample(self, *, include_image: bool = False, include_raw_text: bool = False) -> VisualFrame:
        started = time.perf_counter()
        image = None
        png = None
        at_ms = monotonic_ms()
        window_info = {}
        crop = size = None
        reading = OcrReading((), 0)
        reason = "not_run"
        error_info = None
        capture_method = ""
        capture_attempts = ()
        language = self.config.language
        timings = {"capture_ms": 0.0, "ocr_ms": 0.0}
        max_age = adaptive_max_age_ms(self.latency_ms)
        try:
            if os.name != "nt":
                raise VisualProbeError("ocr_windows_only")
            capture_started = time.perf_counter()
            try:
                image, window, crop, at_ms = _capture(self.config, session=self.capture_session)
                capture_info = getattr(image, "info", {})
                capture_method = capture_info.get("capture_method", "")
                capture_attempts = capture_info.get("capture_attempts", ())
            finally:
                timings["capture_ms"] = round((time.perf_counter() - capture_started) * 1000, 3)
            window_info = {
                "hwnd": window.hwnd, "pid": window.pid,
                "class_name": window.class_name, "client_rect": window.client_bounds,
                "monitor": window.monitor_id, "dpi": window.dpi,
            }
            size = image.size
            geometry = (window.hwnd, window.client_bounds[2] - window.client_bounds[0],
                        window.client_bounds[3] - window.client_bounds[1], window.monitor_id, window.dpi)
            aspect = geometry[1] / geometry[2]
            if self._baseline_ratio is None:
                self._baseline_ratio = aspect
            elif abs(aspect / self._baseline_ratio - 1) > ASPECT_RATIO_TOLERANCE:
                raise VisualProbeError("ocr_recalibration_required")
            if self._geometry is None:
                self._geometry = geometry
            elif self._geometry != geometry:
                # Normalized ROI follows physical resize/DPI/monitor changes.
                # Confirmation restarts across transitions; no stale name carries.
                self.stabilizer.reset()
                self._geometry = geometry
            if include_image or self.config.include_image:
                stream = io.BytesIO()
                image.save(stream, format="PNG")
                png = stream.getvalue()
            if _blank_image(image):
                raise VisualProbeError("ocr_capture_blank")
            ocr_started = time.perf_counter()
            try:
                if self.reader is None:
                    self.reader = WindowsOcr(self.config.language)
                language = self.reader.language
                reading = self.loop.run_until_complete(self.reader.recognize_details(image))
            finally:
                timings["ocr_ms"] = round((time.perf_counter() - ocr_started) * 1000, 3)
            latency = max(0, monotonic_ms() - at_ms)
            # Use the measured current frame to avoid a stale flood during a
            # slow first/full-frame debug sample, then decay after warmup.
            self.latency_ms = max(latency, self.latency_ms * .75)
            max_age = adaptive_max_age_ms(self.latency_ms)
            if latency > max_age:
                raise VisualProbeError("ocr_stale")
            _, reason = parse_ocr_decision(reading.lines)
            if reading.clipped:
                raise VisualProbeError("ocr_name_clipped")
            observation = self.stabilizer.observe(list(reading.lines), at_ms, max_gap_ms=max_age)
        except VisualProbeError as error:
            capture_method = getattr(error, "capture_method", capture_method)
            capture_attempts = getattr(error, "capture_attempts", capture_attempts)
            self.stabilizer.reset()
            reason = error.code
            observation = _unknown(at_ms, error.code)
        except Exception as error:
            from .app_log import exception_details
            error_info = exception_details(error)
            self.stabilizer.reset()
            reason = "ocr_unavailable"
            observation = _unknown(at_ms, reason)
        finally:
            if image is not None:
                image.close()
        timings["total_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return VisualFrame(
            observation=observation,
            raw_lines=reading.lines if (include_raw_text or self.config.dump_ocr_text) else (),
            parser_reason=reason, timings=timings, window=window_info, crop_bounds=crop,
            image_size=size, language=language, max_age_ms=max_age,
            line_count=len(reading.lines), word_count=reading.word_count, image_png=png,
            error_info=error_info, capture_method=capture_method, capture_attempts=capture_attempts)


def _visual_worker(connection, session_id: str, config: VisualSpeakerConfig, verbose: bool = False) -> None:
    """Killable process worker shared by all OCR modes; never writes log files."""
    from .zoom_window import enable_process_dpi_awareness
    if os.name == "nt":
        enable_process_dpi_awareness()
    sampler = None
    try:
        sampler = VisualSampler(config, session_id)
        while True:
            command = connection.recv()
            if command not in ("sample", "preview"):
                return
            connection.send(sampler.sample(
                include_image=command == "preview", include_raw_text=verbose or command == "preview"))
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        if sampler is not None:
            sampler.close()
        connection.close()

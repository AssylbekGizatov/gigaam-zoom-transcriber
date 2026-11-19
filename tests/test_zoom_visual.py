import asyncio
from contextlib import nullcontext
import os
import sys
from types import SimpleNamespace

import pytest

from gigaam_zoom import zoom_visual as module
from gigaam_zoom import zoom_window
from gigaam_zoom.zoom_window import MeetingWindow
from gigaam_zoom.zoom_visual import (
    NameStabilizer, VisualProbeError, VisualSpeakerConfig, WindowsOcr,
    parse_ocr_name, parse_ocr_decision, region_bounds, VisualSampler, OcrReading,
    adaptive_max_age_ms, preferred_ocr_language,
)


@pytest.mark.parametrize("lines,name", [
    (["Иван Петров"], "Иван Петров"), (["Alice Smith"], "Alice Smith"),
    (["  Әлия  "], "Әлия"), (["iPhone (2)"], "iPhone (2)"),
    (["Chloe\u0301"], "Chloé"),
])
def test_single_display_name_preserved(lines, name):
    assert parse_ocr_name(lines) == name


@pytest.mark.parametrize("lines", [
    [], [""], ["Alice", "Bob"], ["Alice\nBob"], ["Alice…"], ["Alice..."],
    ["Alice\x00"], ["Alice\u202e"], ["Mute"], ["Участники"], ["Speaker View"],
    ["--"], ["X" * 161],
])
def test_bad_crops_do_not_create_people(lines):
    assert parse_ocr_name(lines) is None


def test_stability_switch_failure_and_names_are_separate_identities():
    stabilizer = NameStabilizer("session")
    assert stabilizer.observe(["Alice"], 1000).evidence == "ocr_confirming"
    alice = stabilizer.observe(["Alice"], 1500)
    assert alice.display_name == "Alice"
    assert alice.observed_at_ms == 1500  # Confirmation never backfills first reading.
    assert stabilizer.observe(["Bob"], 2000).display_name is None
    bob = stabilizer.observe(["Bob"], 2500)
    assert bob.display_name == "Bob"
    assert bob.source_id != alice.source_id
    assert stabilizer.observe([], 3000).display_name is None
    assert stabilizer.observe(["Bob"], 3500).display_name is None
    assert stabilizer.observe(["Bob"], 4000).source_id == bob.source_id
    stabilizer.reset()
    assert stabilizer.observe(["Bob"], 4500).display_name is None


def test_reading_gaps_clock_regressions_and_sessions_reset_identity():
    first, second = NameStabilizer("one"), NameStabilizer("two")
    for stabilizer in (first, second):
        stabilizer.observe(["Alice"], 1000)
    a, b = first.observe(["Alice"], 1500), second.observe(["Alice"], 1500)
    assert a.source_id != b.source_id
    assert first.observe(["Alice"], 4000).display_name is None
    assert first.observe(["Alice"], 3900).display_name is None
    assert first.observe(["Alice"], 3900).display_name is None


@pytest.mark.parametrize("region", [
    (0, 0, 0, .1), (0, 0, .1, 0), (-.1, 0, .2, .2), (.9, 0, .2, .2),
    (0, .9, .2, .2), (True, 0, .2, .2), (float("nan"), 0, .2, .2),
    (0, 0, float("inf"), .2), (0, 0, .2), "0,0,.2,.2",
])
def test_invalid_regions_rejected(region):
    with pytest.raises(ValueError):
        VisualSpeakerConfig(region).validate()


def test_geometry_handles_negative_monitor_origin_and_window_resize():
    config = VisualSpeakerConfig((.1, .8, .3, .1), client_size=(1000, 500))
    assert region_bounds((-1500, 100, -500, 600), config) == (-1400, 500, -1100, 550)
    assert region_bounds((-1500, 100, 500, 1100), config) == (-1300, 900, -700, 1000)
    assert region_bounds((-1500, 100, -400, 600), config) == (-1390, 500, -1060, 550)
    with pytest.raises(VisualProbeError, match="ocr_recalibration_required"):
        region_bounds((-1500, 100, -200, 600), config)
    with pytest.raises(VisualProbeError, match="ocr_region_too_small"):
        region_bounds((0, 0, 100, 100), VisualSpeakerConfig((0, 0, .01, .01)))


def test_capture_closes_image_when_window_changes(monkeypatch):
    pytest.importorskip("PIL.ImageGrab")
    from PIL import ImageGrab
    closed = []
    fake_image = SimpleNamespace(close=lambda: closed.append(True))
    positions = iter([MeetingWindow(1, 2, "Zoom Meeting", "class", (0, 0, 1000, 500), "monitor1", 96),
                      MeetingWindow(1, 2, "Zoom Meeting", "class", (10, 0, 1010, 500), "monitor1", 96)])
    monkeypatch.setattr(zoom_window, "physical_pixels", nullcontext)
    monkeypatch.setattr(zoom_window, "find_meeting_window", lambda: next(positions))
    monkeypatch.setattr(zoom_window, "ensure_region_uncovered", lambda *args: None)
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: fake_image)
    with pytest.raises(VisualProbeError, match="ocr_window_changed"):
        module._capture(VisualSpeakerConfig((0, 0, .5, .1)))
    assert closed == [True]


@pytest.mark.parametrize("clipped", [False, True])
def test_native_ocr_bitmap_contract_and_cleanup(monkeypatch, clipped):
    Image = pytest.importorskip("PIL.Image")
    closed, calls = [], []

    class Writer:
        def write_bytes(self, data):
            calls.append(("bytes", len(data)))

        def detach_buffer(self):
            return "buffer"

        def close(self):
            closed.append("writer")

    class Bitmap:
        @staticmethod
        def create_copy_with_alpha_from_buffer(buffer, format, width, height, alpha):
            calls.append((buffer, format, width, height, alpha))
            return SimpleNamespace(close=lambda: closed.append("bitmap"))

    class Engine:
        async def recognize_async(self, bitmap):
            box = SimpleNamespace(x=0 if clipped else 6, y=6, width=20, height=12)
            return SimpleNamespace(lines=[SimpleNamespace(text="Alice", words=[SimpleNamespace(bounding_rect=box)])])

    monkeypatch.setitem(sys.modules, "winrt.windows.graphics.imaging", SimpleNamespace(
        BitmapAlphaMode=SimpleNamespace(IGNORE=2), BitmapPixelFormat=SimpleNamespace(BGRA8=87),
        SoftwareBitmap=Bitmap))
    monkeypatch.setitem(sys.modules, "winrt.windows.storage.streams", SimpleNamespace(DataWriter=Writer))
    reader = WindowsOcr.__new__(WindowsOcr)
    reader.engine, reader.max_dimension = Engine(), 2600
    with Image.new("RGB", (80, 20), "white") as image:
        if clipped:
            with pytest.raises(VisualProbeError, match="ocr_name_clipped"):
                asyncio.run(reader.recognize(image))
        else:
            assert asyncio.run(reader.recognize(image)) == ["Alice"]
    assert calls == [("bytes", 240 * 60 * 4), ("buffer", 87, 240, 60, 2)]
    assert closed == ["bitmap", "writer"]


def test_native_ocr_unavailable_language_is_explicit(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "winrt.runtime", SimpleNamespace(
        ApartmentType=SimpleNamespace(MULTI_THREADED=1),
        init_apartment=lambda mode: calls.append("init"),
        uninit_apartment=lambda: calls.append("uninit")))
    monkeypatch.setitem(sys.modules, "winrt.windows.globalization", SimpleNamespace(Language=lambda tag: tag))
    monkeypatch.setitem(sys.modules, "winrt.windows.media.ocr", SimpleNamespace(
        OcrEngine=SimpleNamespace(try_create_from_language=lambda language: None)))
    with pytest.raises(VisualProbeError, match="ocr_language_unavailable"):
        WindowsOcr("ru-RU")
    assert calls == ["init", "uninit"]


def test_worker_invalidates_confirmed_name_after_capture_failure(monkeypatch):
    class Connection:
        def __init__(self):
            self.commands = iter(["sample"] * 5 + ["stop"])
            self.results = []
            self.closed = False

        def recv(self):
            return next(self.commands)

        def send(self, result):
            self.results.append(result)

        def close(self):
            self.closed = True

    times = iter([1000, 1500, 2000, 2500, 3000])
    clock = [1000]

    def capture(config):
        at_ms = next(times)
        clock[0] = at_ms
        if at_ms == 2000:
            raise VisualProbeError("ocr_region_occluded")
        return (SimpleNamespace(close=lambda: None, size=(100, 20)),
                MeetingWindow(1, 2, "Zoom Meeting", "class", (0, 0, 100, 100), "monitor1", 96),
                (0, 0, 100, 20), at_ms)

    class Reader:
        def __init__(self, language):
            self.language = language

        async def recognize_details(self, image):
            return OcrReading(("Alice",), 1)

        def close(self):
            pass

    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module, "_capture", capture)
    monkeypatch.setattr(module, "monotonic_ms", lambda: clock[0])
    monkeypatch.setattr(module, "WindowsOcr", Reader)
    monkeypatch.setattr(module, "_blank_image", lambda image: False)
    connection = Connection()
    module._visual_worker(connection, "session", VisualSpeakerConfig((0, 0, .5, .1)))
    assert [r.observation.display_name for r in connection.results] == [None, "Alice", None, None, "Alice"]
    assert connection.results[2].observation.evidence == "ocr_region_occluded"
    assert connection.closed


@pytest.mark.parametrize("lines,expected", [
    (["Иван Петров (Организатор)"], "Иван Петров"),
    (["Mute Alice (Host)"], "Alice"), (["Alice", "Mute"], "Alice"),
    (["Участники", "Әлия"], "Әлия"),
])
def test_parser_removes_exact_zoom_chrome_before_name_decision(lines, expected):
    assert parse_ocr_decision(lines) == (expected, "accepted")


def test_parser_reports_ambiguity_and_rejection_reasons():
    assert parse_ocr_decision(["Alice", "Bob"])[1] == "ocr_name_ambiguous"
    assert parse_ocr_decision(["Mute", "Chat"])[1] == "ocr_name_ui_only"
    assert parse_ocr_decision(["Иван…"])[1] == "ocr_name_clipped"
    assert parse_ocr_decision(["Recording", "Запись"])[1] == "ocr_name_ui_only"
    assert parse_ocr_decision(["12345"])[1] == "ocr_name_no_letters"


def test_freshness_budget_is_adaptive_and_bounded():
    assert adaptive_max_age_ms(50) == 2000
    assert adaptive_max_age_ms(2500) == 6500
    assert adaptive_max_age_ms(20000) == 10000
    assert adaptive_max_age_ms(float("nan")) == 2000


def test_installed_language_preference():
    assert preferred_ocr_language(["en-US", "ru"]) == "ru"
    assert preferred_ocr_language([]) == ""
    assert preferred_ocr_language(["en-US", "ru-RU"]) == "ru-RU"
    assert preferred_ocr_language(["de-DE", "en-US"]) == "en-US"
    assert preferred_ocr_language(["de-DE"]) == "de-DE"


def _sampler_fixture(monkeypatch, *, blank=False, latency=2500, clipped=False):
    Image = pytest.importorskip("PIL.Image")
    clock = [1000]
    window = [MeetingWindow(1, 2, "Zoom Meeting", "class", (0, 0, 1000, 500), "monitor1", 96)]
    calls = []

    def capture(config):
        crop = region_bounds(window[0].client_bounds, config)
        image = Image.new("RGB", (crop[2] - crop[0], crop[3] - crop[1]), "black")
        if not blank:
            for x in range(20, 80):
                image.putpixel((x, 12), (255, 255, 255))
        return image, window[0], crop, clock[0]

    class Reader:
        def __init__(self, language):
            self.language = language
            calls.append("init")

        async def recognize_details(self, image):
            clock[0] += latency
            return OcrReading(("Mute Alice (Host)",), 3, clipped=clipped)

        def close(self):
            calls.append("close")

    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module, "_capture", capture)
    monkeypatch.setattr(module, "monotonic_ms", lambda: clock[0])
    monkeypatch.setattr(module, "WindowsOcr", Reader)
    return clock, window, calls


def test_slow_frames_keep_capture_time_and_confirm_with_adaptive_age(monkeypatch):
    clock, _, calls = _sampler_fixture(monkeypatch)
    sampler = VisualSampler(VisualSpeakerConfig((0, 0, .5, .1)), "session")
    try:
        first = sampler.sample()
        assert first.observation.observed_at_ms == 1000
        assert first.observation.evidence == "ocr_confirming"
        assert first.max_age_ms == 6500
        assert first.raw_lines == () and first.image_png is None
        clock[0] += 500
        second = sampler.sample(include_image=True, include_raw_text=True)
        assert second.observation.display_name == "Alice"
        assert second.observation.observed_at_ms == 4000
        assert second.raw_lines == ("Mute Alice (Host)",)
        assert second.parser_reason == "accepted"
        assert second.line_count == 1 and second.word_count == 3
        assert second.image_png.startswith(b"\x89PNG")
        assert second.window["dpi"] == 96
        assert "image_png" not in second.diagnostic_dict()
    finally:
        sampler.close()
    assert calls == ["init", "close"]


def test_uniform_capture_is_distinct_from_ocr_no_text(monkeypatch):
    _, _, calls = _sampler_fixture(monkeypatch, blank=True)
    sampler = VisualSampler(VisualSpeakerConfig((0, 0, .5, .1)), "session")
    try:
        result = sampler.sample(include_image=True)
        assert result.observation.evidence == "ocr_capture_blank"
        assert result.image_png is not None
        assert result.line_count == 0
        assert calls == []  # No OCR model invocation for an empty capture.
    finally:
        sampler.close()


def test_clipped_full_frame_debug_keeps_unfiltered_lines_and_parser_reason(monkeypatch):
    _sampler_fixture(monkeypatch, clipped=True)
    sampler = VisualSampler(VisualSpeakerConfig((0, 0, 1, 1), dump_ocr_text=True), "session")
    try:
        result = sampler.sample()
        assert result.observation.evidence == "ocr_name_clipped"
        assert result.raw_lines == ("Mute Alice (Host)",)
        assert result.parser_reason == "ocr_name_clipped"
        assert result.line_count == 1 and result.word_count == 3
    finally:
        sampler.close()


def test_sampler_recomputes_normalized_roi_for_resize_monitor_dpi_and_recreated_window(monkeypatch):
    clock, window, _ = _sampler_fixture(monkeypatch, latency=50)
    sampler = VisualSampler(VisualSpeakerConfig((0, 0, .5, .1), client_size=(1000, 500)), "session")
    try:
        sampler.sample()
        clock[0] += 750
        window[0] = MeetingWindow(1, 2, "Zoom Meeting", "class", (100, 0, 1100, 500), "monitor1", 96)
        assert sampler.sample().observation.display_name == "Alice"  # Move within same monitor.
        for changed in (
            MeetingWindow(1, 2, "Zoom Meeting", "class", (100, 0, 1600, 750), "monitor2", 144),
            MeetingWindow(1, 2, "Zoom Meeting", "class", (-1000, 0, 100, 500), "monitor1", 96),
            MeetingWindow(9, 3, "Zoom Meeting", "class", (-1000, 0, 100, 500), "monitor1", 96),
        ):
            clock[0] += 750
            window[0] = changed
            transition = sampler.sample()
            assert transition.observation.evidence == "ocr_confirming"
            assert transition.crop_bounds == region_bounds(changed.client_bounds, sampler.config)
            crop = transition.crop_bounds
            assert transition.image_size == (crop[2] - crop[0], crop[3] - crop[1])
            clock[0] += 750
            assert sampler.sample().observation.display_name == "Alice"
        window[0] = MeetingWindow(9, 3, "Zoom Meeting", "class", (0, 0, 1600, 500), "monitor1", 96)
        assert sampler.sample().observation.evidence == "ocr_recalibration_required"
    finally:
        sampler.close()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows OCR integration requires Windows")
def test_windows_native_ocr_smoke():
    # Fail on missing wheels/API mismatch on Windows CI. Only an absent OS
    # language pack is an expected platform capability skip.
    from PIL import Image, ImageDraw, ImageFont
    try:
        reader = WindowsOcr("en-US")
    except VisualProbeError as error:
        if error.code == "ocr_language_unavailable":
            pytest.skip("Windows en-US OCR language capability is not installed")
        raise
    try:
        with Image.new("RGB", (360, 100), "black") as image:
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype("arial.ttf", 32)
            draw.text((24, 20), "Alice Smith", fill="white", font=font)
            lines = asyncio.run(reader.recognize(image))
            assert parse_ocr_name(lines) == "Alice Smith"
    finally:
        reader.close()

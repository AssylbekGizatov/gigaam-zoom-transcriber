import asyncio
from contextlib import nullcontext
import os
import sys
from types import SimpleNamespace

import pytest

from gigaam_zoom import zoom_visual as module
from gigaam_zoom.zoom_visual import (
    NameStabilizer, VisualProbeError, VisualSpeakerConfig, WindowsOcr,
    parse_ocr_name, region_bounds,
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
    with pytest.raises(VisualProbeError, match="ocr_recalibration_required"):
        region_bounds((-1500, 100, -400, 600), config)
    with pytest.raises(VisualProbeError, match="ocr_region_too_small"):
        region_bounds((0, 0, 100, 100), VisualSpeakerConfig((0, 0, .01, .01)))


def test_capture_closes_image_when_window_changes(monkeypatch):
    pytest.importorskip("PIL.ImageGrab")
    from PIL import ImageGrab
    closed = []
    fake_image = SimpleNamespace(close=lambda: closed.append(True))
    positions = iter([(1, (0, 0, 1000, 500)), (1, (10, 0, 1010, 500))])
    monkeypatch.setattr(module, "_physical_pixels", nullcontext)
    monkeypatch.setattr(module, "meeting_client_bounds", lambda: next(positions))
    monkeypatch.setattr(module, "_check_uncovered", lambda *args: None)
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
            raise VisualProbeError("ocr_window_not_foreground")
        return SimpleNamespace(close=lambda: None), (0, 0, 100, 100), at_ms

    class Reader:
        def __init__(self, language):
            pass

        async def recognize(self, image):
            return ["Alice"]

        def close(self):
            pass

    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module, "_capture", capture)
    monkeypatch.setattr(module, "monotonic_ms", lambda: clock[0])
    monkeypatch.setattr(module, "WindowsOcr", Reader)
    connection = Connection()
    module._visual_worker(connection, "session", VisualSpeakerConfig((0, 0, .5, .1)))
    assert [r.display_name for r in connection.results] == [None, "Alice", None, None, "Alice"]
    assert connection.results[2].evidence == "ocr_window_not_foreground"
    assert connection.closed


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

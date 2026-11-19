from contextlib import nullcontext
from dataclasses import replace
import io
import json
import sys
import time

from PIL import Image, ImageDraw
import pytest

from gigaam_zoom import zoom_capture as capture
from gigaam_zoom import zoom_window
from gigaam_zoom.zoom_visual import VisualSpeakerConfig, _capture, VisualProbeError


def content_image(size=(100, 30)):
    image = Image.new("RGB", size, "black")
    ImageDraw.Draw(image).rectangle((5, 5, 35, 20), fill="white")
    return image


@pytest.fixture
def selected(monkeypatch):
    window = zoom_window.MeetingWindow(1, 2, "Zoom Meeting", "class", (-900, 100, 100, 600), "m1", 144)
    monkeypatch.setattr(zoom_window, "physical_pixels", nullcontext)
    monkeypatch.setattr(zoom_window, "find_meeting_window", lambda: window)
    return window


def test_full_window_and_dwm_frames_map_same_client_roi_on_negative_monitor():
    client = (-900, 100, 100, 600)
    frames = ((-908, 70, 108, 608), (-900, 70, 100, 600))
    crop = (-800, 500, -500, 550)
    assert capture.map_frame_to_client_crop((1016, 538), frames, client, crop) == (108, 430, 408, 480)
    assert capture.map_frame_to_client_crop((1000, 530), frames, client, crop) == (100, 430, 400, 480)


@pytest.mark.parametrize("size,frames,client,crop", [
    ((500, 250), ((0, 0, 1000, 500),), (0, 0, 1000, 500), (10, 10, 100, 30)),
    ((100, 100), ((0, 0, 100, 100), (1, 0, 101, 100)), (10, 10, 90, 90), (10, 10, 50, 40)),
    ((100, 100), ((0, 0, 100, 100),), (10, 10, 90, 90), (0, 0, 50, 40)),
])
def test_unknown_scale_ambiguous_bounds_and_crop_outside_client_are_rejected(size, frames, client, crop):
    with pytest.raises(capture.CaptureError, match="capture_geometry_mismatch"):
        capture.map_frame_to_client_crop(size, frames, client, crop)


def test_covered_window_uses_wgc_without_screen_occlusion_check(monkeypatch, selected):
    monkeypatch.setattr(zoom_window, "ensure_region_uncovered", lambda *args: pytest.fail("Screen check used for WGC"))
    session = capture.CaptureSession()
    monkeypatch.setattr(session, "_native_capture", lambda *args: (content_image(), 1000))
    result = session.capture(selected, (-800, 500, -700, 530))
    assert result.method == "wgc"
    assert result.attempts == ({"method": "wgc", "evidence": "capture_ok"},)
    result.image.close()
    session.close()


def test_blank_wgc_reaches_printwindow_and_closes_rejected_image(monkeypatch, selected):
    blank = Image.new("RGB", (100, 30), "black")
    now = 50.0
    monkeypatch.setattr(capture.time, "monotonic", lambda: now)
    calls = []
    session = capture.CaptureSession()
    def native(method, *args):
        calls.append(method)
        return (blank if method == "wgc" and now == 50.0 else content_image()), 1000
    monkeypatch.setattr(session, "_native_capture", native)
    result = session.capture(selected, (-800, 500, -700, 530))
    assert result.method == "printwindow"
    assert calls == ["wgc", "printwindow"]
    with pytest.raises(ValueError):
        blank.getpixel((0, 0))
    result.image.close()
    result = session.capture(selected, (-800, 500, -700, 530))
    assert calls == ["wgc", "printwindow", "printwindow"]
    assert result.attempts[0]["evidence"] == "capture_backoff"
    result.image.close()
    now += 2.1
    result = session.capture(selected, (-800, 500, -700, 530))
    assert result.method == "wgc"
    result.image.close()
    session.close()


def test_native_errors_fall_back_to_screen_and_require_uncovered_roi(monkeypatch, selected):
    session = capture.CaptureSession()
    monkeypatch.setattr(session, "_native_capture", lambda *args: (_ for _ in ()).throw(capture.CaptureError("capture_native_error")))
    checked = []
    monkeypatch.setattr(zoom_window, "ensure_region_uncovered", lambda *args: checked.append(args))
    from PIL import ImageGrab
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: content_image())
    result = session.capture(selected, (-800, 500, -700, 530))
    assert result.method == "screen_dc"
    assert len(checked) == 2
    result.image.close()
    session.close()


def test_covered_roi_is_unknown_if_native_methods_fail(monkeypatch, selected):
    session = capture.CaptureSession()
    monkeypatch.setattr(session, "_native_capture", lambda *args: (_ for _ in ()).throw(capture.CaptureError("capture_native_error")))
    def covered(*args):
        raise zoom_window.WindowProbeError("ocr_region_occluded")
    monkeypatch.setattr(zoom_window, "ensure_region_uncovered", covered)
    with pytest.raises(capture.CaptureError, match="ocr_region_occluded") as error:
        session.capture(selected, (-800, 500, -700, 530))
    assert error.value.method == "screen_dc"
    assert len(error.value.attempts) == 3
    session.close()


def test_minimized_window_never_enters_any_capture_provider(monkeypatch):
    monkeypatch.setattr(zoom_window, "physical_pixels", nullcontext)
    def minimized():
        raise zoom_window.WindowProbeError("ocr_window_minimized")
    monkeypatch.setattr(zoom_window, "find_meeting_window", minimized)
    monkeypatch.setattr(capture.CaptureSession, "capture", lambda *args: pytest.fail("Minimized window captured"))
    with pytest.raises(VisualProbeError, match="ocr_window_minimized"):
        _capture(VisualSpeakerConfig((0, 0, .5, .1)))


def test_recreated_window_during_native_call_is_rejected_and_image_closed(monkeypatch, selected):
    session = capture.CaptureSession()
    image = content_image()
    def native(*args):
        monkeypatch.setattr(zoom_window, "find_meeting_window", lambda: replace(selected, hwnd=77, pid=88))
        return image, 1000
    monkeypatch.setattr(session, "_native_capture", native)
    with pytest.raises(capture.CaptureError, match="ocr_window_changed"):
        session.capture(selected, (-800, 500, -700, 530))
    with pytest.raises(ValueError):
        image.getpixel((0, 0))
    session.close()


def test_actual_hung_helper_is_killed_and_printwindow_fallback_reached(monkeypatch, selected):
    # The first request really blocks in a separate interpreter. A fresh helper
    # handles PrintWindow after termination; no screen fallback is needed.
    output = io.BytesIO()
    image = content_image()
    image.save(output, format="PNG")
    image.close()
    code = '''import sys,json,time,base64
for line in sys.stdin.buffer:
    request=json.loads(line)
    if request['method']=='wgc': time.sleep(60)
    data=base64.b64decode(sys.argv[1])
    sys.stdout.buffer.write(json.dumps({'bytes':len(data),'at_ms':1000}).encode()+b'\\n'+data)
    sys.stdout.buffer.flush()
'''
    import base64
    session = capture.CaptureSession(native_timeout=.3, _command=[sys.executable, "-u", "-c", code,
                                                                base64.b64encode(output.getvalue()).decode()])
    started = time.monotonic()
    try:
        result = session.capture(selected, (-800, 500, -700, 530))
        assert result.method == "printwindow"
        assert result.attempts[0]["evidence"] == "capture_wgc_timeout"
        assert time.monotonic() - started < 3
        pid = session._process.pid
        result.image.close()
        again = session.capture(selected, (-800, 500, -700, 530))
        assert session._process.pid == pid  # Persistent helper; no per-sample spawn.
        again.image.close()
    finally:
        process = session._process
        session.close()
        assert process is None or process.poll() is not None


def test_hung_printwindow_also_reaches_screen_fallback(monkeypatch, selected):
    code = "import sys,json,time\nfor line in sys.stdin.buffer: time.sleep(60)"
    session = capture.CaptureSession(native_timeout=.2, _command=[sys.executable, "-u", "-c", code])
    monkeypatch.setattr(session, "_screen_capture", lambda *args: (content_image(), 1000))
    try:
        result = session.capture(selected, (-800, 500, -700, 530))
        assert result.method == "screen_dc"
        assert [a["evidence"] for a in result.attempts] == ["capture_wgc_timeout", "capture_printwindow_timeout", "capture_ok"]
        assert session._process is None
        result.image.close()
    finally:
        session.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Real PrintWindow client mapping requires Windows")
def test_native_printwindow_maps_client_and_reads_pixels_behind_overlay(covered_capture_window):
    """Actual native provider; screen pixels are blue but the HWND is red."""
    from PIL import ImageGrab
    target = covered_capture_window
    with zoom_window.physical_pixels():
        bounds = zoom_window.window_frame_bounds(target)
        client = zoom_window._api().client_bounds(target)
        with capture._print_window(target) as frame:
            texture_crop = capture.map_frame_to_client_crop(frame.size, bounds, client, client)
            with frame.crop(texture_crop) as cropped:
                assert cropped.size == (client[2] - client[0], client[3] - client[1])
                red, green, blue = cropped.getpixel((cropped.width // 2, cropped.height // 2))
                assert red > 170 and green < 80 and blue < 80, (red, green, blue)
        with ImageGrab.grab(bbox=client, all_screens=True, include_layered_windows=True) as screen:
            red, green, blue = screen.getpixel((screen.width // 2, screen.height // 2))
            assert blue > 170 and red < 80, "Fixture must really cover the target's screen pixels"


# Shared fixture owns only its two temporary windows, never the user's Zoom.
from test_zoom_wgc import covered_capture_window

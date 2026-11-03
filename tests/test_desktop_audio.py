import threading
import time

import numpy as np
import pytest

from gigaam_zoom.desktop_audio import DesktopCapture, StreamingResampler, _SourceClock


@pytest.mark.parametrize("rate", [8000, 16000, 44100, 48000, 192000])
def test_resampler_chunk_boundaries_match_one_block(rate):
    rng = np.random.default_rng(12)
    pcm = rng.integers(-20000, 20000, (rate // 2, 2), dtype=np.int16).astype("<i2").tobytes()
    expected = StreamingResampler(rate, 2).process(pcm)
    converter = StreamingResampler(rate, 2)
    pieces = []
    for start in range(0, len(pcm), 101 * 4):
        pieces.append(converter.process(pcm[start:start + 101 * 4]))
    assert b"".join(pieces) == expected
    assert abs(len(expected) // 2 - 8000) <= 1
    assert len(converter._history) <= 126
    assert converter._last.size == 1


def test_stereo_downmix_and_int16_endianness():
    pcm = np.array([[30000, -30000], [-32000, 32000], [20000, 10000]], dtype="<i2").tobytes()
    assert np.frombuffer(StreamingResampler(16000, 2).process(pcm), dtype="<i2").tolist() == [0, 0, 15000]


def test_downsampler_rejects_above_nyquist_alias():
    rate = 48000
    clock = np.arange(rate) / rate
    def result(hz):
        pcm = np.rint(np.sin(2 * np.pi * hz * clock) * 24000).astype("<i2").tobytes()
        return np.frombuffer(StreamingResampler(rate, 1).process(pcm), dtype="<i2")[100:].astype(float)
    speech, ultrasonic = result(1000), result(12000)
    assert np.sqrt(np.mean(speech ** 2)) > 16000
    assert np.sqrt(np.mean(ultrasonic ** 2)) < 100


def test_resampler_validation_and_no_audio():
    converter = StreamingResampler(48000, 2)
    assert converter.process(b"") == b""
    with pytest.raises(ValueError):
        converter.process(b"123")
    with pytest.raises(ValueError):
        converter.process(bytes(48001 * 4))
    for rate, channels in [(1, 2), (48000, 0), (48000, 33)]:
        with pytest.raises(ValueError):
            StreamingResampler(rate, channels)


def test_source_clock_does_not_follow_callback_jitter_or_wall_clock():
    clock = _SourceClock(48000, 100000, 5)
    assert clock.stamp(960, 5.02) == 100000
    assert clock.stamp(960, 5.08) == 100020
    assert clock.stamp(960, 5.10) == 100040
    assert clock.stamp(960, 6.02, discontinuity=True) == 101000
    assert clock.stamp(960, 6.04) == 101020


def test_source_clock_preserves_unreported_device_stall():
    clock = _SourceClock(48000, 100000, 5)
    assert clock.stamp(960, 5.02) == 100000
    assert not clock.last_discontinuity
    assert clock.stamp(960, 6.02) == 101000
    assert clock.last_discontinuity
    assert clock.stamp(960, 6.04) == 101020
    assert not clock.last_discontinuity


class _PA:
    paComplete = 1
    paContinue = 0


def callback_capture(on_audio=None, on_event=None):
    capture = DesktopCapture(on_audio=on_audio, on_event=on_event)
    capture.origin_ms = 1000
    capture._origin_clock = time.perf_counter()
    return capture, capture._callback("remote", 48000, 2, _PA)


def test_callback_queue_is_bounded_and_surfaces_loss():
    capture, callback = callback_capture()
    block = bytes(960 * 4)
    for _ in range(1000):
        assert callback(block, 960, {}, 0) == (None, _PA.paContinue)
    assert capture.metrics["pending_seconds"] <= 2
    assert capture.metrics["dropped_seconds"] > 17
    assert capture._queue.qsize() < 128
    capture.stop()
    assert capture._queue.empty()
    assert capture.metrics["pending_seconds"] == 0
    assert callback(block, 960, {}, 0) == (None, _PA.paComplete)


def test_worker_pcm_callback_and_drain():
    packets, events = [], []
    capture, callback = callback_capture(lambda *p: packets.append(p), lambda *e: events.append(e))
    callback(bytes(960 * 4), 960, {}, 0)
    capture._stopping.set()
    capture._run()
    assert len(packets) == 1
    assert packets[0][0] == "remote"
    assert len(packets[0][1]) == 320 * 2
    assert packets[0][3] == 16000
    assert events[-1][0] == "capture_stopped"


def test_hardware_overflow_reports_gap_and_worker_errors_stop_capture():
    events = []
    capture, callback = callback_capture(on_event=lambda *e: events.append(e))
    callback(bytes(960 * 4), 960, {}, 2)
    capture._stopping.set()
    capture._run()
    assert any(e[0] == "capture_gap" and e[1]["hardware_overflows"] == 1 for e in events)
    def fail(*args):
        raise RuntimeError("private audio should not be in the diagnostic")
    capture, callback = callback_capture(fail, lambda *e: events.append(e))
    callback(bytes(960 * 4), 960, {}, 0)
    capture._stopping.set()
    capture._run()
    assert capture.metrics["failed"]
    assert any(e[0] == "capture_error" for e in events)
    assert "private audio" not in repr(events)


def test_dropped_blocks_preserve_clock_gap():
    packets = []
    capture, callback = callback_capture(lambda *p: packets.append(p))
    callback(bytes(960 * 4), 960, {}, 0)
    # Force queue overflow, then resume; timestamps must include missing 20 ms.
    with capture._lock:
        capture._pending = 2
    callback(bytes(960 * 4), 960, {}, 0)
    with capture._lock:
        capture._pending = .02
    callback(bytes(960 * 4), 960, {}, 0)
    capture._stopping.set()
    capture._run()
    assert len(packets) == 2
    assert packets[1][2] - packets[0][2] == 40

"""Capture-clock evidence, delayed label transitions and ASR backlog regression."""
from dataclasses import replace
import threading
import time

import numpy as np
import pytest

from gigaam_zoom.desktop_attribution import DesktopSegmenter, Timeline, UNKNOWN
from gigaam_zoom.models import AudioPacket, Word
from gigaam_zoom.pipeline import Pipeline
from gigaam_zoom.speaker_observer import SpeakerObservation
from gigaam_zoom.store import Store


def reading(at, name="Alice", *, confirmed=True, confidence=None):
    return SpeakerObservation(at, "local:zoom-ocr:" + name if confirmed else "local:zoom-ocr:unknown",
        name if confirmed else None, "ocr_speaker_view_hint" if confirmed else "ocr_confirming",
        confidence=confidence, recognized_name=None if confirmed else name)


def active(timeline, start, duration, source="remote"):
    timeline.add_audio(source, start, np.full(round(duration * 16000), 500), 16000)


def test_actual_a_to_b_switch_with_one_second_display_delay():
    timeline = Timeline()
    active(timeline, 10000, 6)
    # Alice speaks before 12s, Bob from 12s; Zoom keeps Alice until 13s.
    for at in (10000, 10750, 11500, 12250):
        timeline.add(reading(at))
    timeline.add(reading(13000, "Bob", confirmed=False))
    for at in (13750, 14500, 15250, 16000):
        timeline.add(reading(at, "Bob"))
    # This word precedes the first B screen sample, yet its stable future
    # support clearly dominates. The very beginning stays ambiguous.
    assert timeline.resolve(12600, 12800).display_name == "Bob"
    assert timeline.resolve(12050, 12200) == UNKNOWN
    assert timeline.decide(12050, 12200).reason == "speaker_conflict"


def test_new_name_after_short_turn_can_confirm_it_but_cannot_cross_next_turn():
    timeline = Timeline()
    active(timeline, 10000, 1)
    active(timeline, 12000, .5)
    active(timeline, 16000, 1)
    for at in (10000, 10750, 11500, 12250):
        timeline.add(reading(at))
    timeline.add(reading(13000, "Bob", confirmed=False))
    timeline.add(reading(13750, "Bob"))
    timeline.add(reading(14500, "Bob"))
    assert timeline.resolve(12100, 12300).display_name == "Bob"
    # A short earlier turn must not borrow B readings from the next turn.
    separate = Timeline()
    active(separate, 10000, .5)
    active(separate, 11000, 2)
    separate.add(reading(11100, "Bob", confirmed=False))
    separate.add(reading(11850, "Bob"))
    assert separate.resolve(10100, 10300) == UNKNOWN


def test_new_unconfirmed_candidate_vetoes_old_winner_without_becoming_a_name():
    timeline = Timeline()
    active(timeline, 10000, 5)
    for at in (10000, 10750, 11500, 12250):
        timeline.add(reading(at))
    timeline.add(reading(13000, "Bob", confirmed=False))
    decision = timeline.decide(12000, 12200)
    assert decision.identity == UNKNOWN
    assert decision.reason == "speaker_conflict"
    singleton = Timeline()
    active(singleton, 10000, 5)
    singleton.add(reading(13000, "Bob", confirmed=False))
    assert singleton.resolve(12000, 12200) == UNKNOWN


def test_capture_age_not_decode_time_and_raw_confidence_stays_optional(monkeypatch):
    timeline = Timeline()
    active(timeline, 10000, 4)
    for at in (10000, 10750, 11500, 12250):
        timeline.add(reading(at))
    assert timeline._entries[0].confidence is None
    assert timeline._entries[0].status == "ok"
    monkeypatch.setattr("gigaam_zoom.desktop_attribution.monotonic_ms", lambda: 999999999)
    assert timeline.resolve(10400, 10600).display_name == "Alice"
    low = Timeline()
    active(low, 10000, 3)
    for at in (10000, 10750, 11500):
        low.add(reading(at, confidence=0))
    assert low.resolve(10400, 10600) == UNKNOWN


def test_minimized_or_conflicting_context_is_never_gap_fill_eligible():
    timeline = Timeline()
    active(timeline, 10000, 5)
    timeline.add(reading(10000))
    timeline.add(reading(10750))
    timeline.add(SpeakerObservation(11000, "local:zoom-ocr:unknown", None, "ocr_window_minimized"))
    timeline.add(SpeakerObservation(11200, "local:zoom-ocr:unknown", None, "ocr_confirming"))
    timeline.add(reading(12000))
    timeline.add(reading(12750))
    assert timeline.decide(11100, 11400).reason == "explicit_ocr_failure"


def test_grouped_words_recheck_mic_overlap_between_words_and_log_once(monkeypatch):
    timeline = Timeline()
    active(timeline, 10000, 4)
    active(timeline, 10620, .06, "microphone")
    for at in (10000, 10750, 11500, 12250):
        timeline.add(reading(at))
    calls = []
    monkeypatch.setattr("gigaam_zoom.desktop_attribution.log_attribution", lambda *args, **kwargs: calls.append((args, kwargs)))
    segment = DesktopSegmenter(timeline)("local:remote", "epoch", "", [
        Word(.4, .6, "one"), Word(.7, .9, "two")], 10000)[0]
    assert segment.attribution == "unattributed_mix"
    assert segment.attribution_reason == "microphone_overlap"
    assert len(calls) == 1 and calls[0][1]["segment_id"] == segment.id
    assert calls[0][0][3] is None


def test_support_logged_for_whole_segment_without_double_counting_words(monkeypatch):
    timeline = Timeline()
    active(timeline, 10000, 5)
    for at in (10000, 10750, 11500, 12250, 13000):
        timeline.add(reading(at))
    calls = []
    monkeypatch.setattr("gigaam_zoom.desktop_attribution.log_attribution", lambda *args, **kwargs: calls.append((args, kwargs)))
    segments = DesktopSegmenter(timeline)("local:remote", "epoch", "", [
        Word(.4, .6, "one"), Word(.7, .9, "two")], 10000)
    assert len(segments) == len(calls) == 1
    assert calls[0][0][2] == timeline.decide(10400, 10900).candidates


def test_waits_once_for_capture_lookahead_and_close_unblocks():
    timeline = Timeline()
    active(timeline, 1000, 4)
    timeline.add(reading(1000))
    timeline.add(reading(1750))
    finished = threading.Event()
    result = []
    waits = []
    original_wait = timeline.wait_for_lookahead
    def measured_wait(*args):
        waits.append(args)
        return original_wait(*args)
    timeline.wait_for_lookahead = measured_wait
    def run():
        result.extend(DesktopSegmenter(timeline, lookahead_timeout=1)(
            "local:remote", "e", "", [Word(.4, .6, "one"), Word(.7, .9, "two")], 1000))
        finished.set()
    thread = threading.Thread(target=run)
    thread.start()
    assert not finished.wait(.05)
    timeline.add(reading(2500))
    timeline.add(reading(3500))
    assert finished.wait(1)
    thread.join(1)
    assert len(result) == 1 and result[0].display_name == "Alice"
    assert len(waits) == 1
    # A final flush cannot wait forever after the observer has stopped.
    stopped = Timeline()
    active(stopped, 1000, 4)
    stopped.add(reading(1000))
    waiting = threading.Thread(target=lambda: stopped.wait_for_lookahead(1400, 1600, 10))
    waiting.start()
    stopped.close()
    waiting.join(.5)
    assert not waiting.is_alive()


def test_pins_preserve_slow_asr_backlog_but_do_not_retain_unaccepted_hours(tmp_path):
    timeline = Timeline()
    active(timeline, 10000, 14)
    for at in range(10000, 25001, 750):
        timeline.add(reading(at))
    entered, release = threading.Event(), threading.Event()
    class SlowEngine:
        def transcribe_words(self, pcm, rate):
            entered.set()
            assert release.wait(3)
            return [Word(1.3, 1.6, "captured")]
    store = Store(tmp_path / "backlog.sqlite")
    pipeline = Pipeline(store, SlowEngine(), origin_ms=10000, max_pending_seconds=24,
                        segmenter=DesktopSegmenter(timeline))
    pipeline.start()
    try:
        for start, seconds in ((10000, 8), (18000, 6)):
            assert pipeline.feed(AudioPacket("local:remote", "", start,
                np.full(seconds * 16000, 500, dtype="<i2").tobytes(), epoch="e"))
        assert entered.wait(1)
        # Advance capture metadata by several minutes while inference is
        # blocked. No sleep and no new accepted PCM: only the old pins survive.
        for at in range(200000, 400001, 1000):
            timeline.add(reading(at, "Bob"))
        timeline.add_audio("remote", 400000, np.zeros(320, dtype="<i2"), 16000)
        assert len(timeline._entries) < 100
        assert timeline.resolve(11300, 11600).display_name == "Alice"
        release.set()
        pipeline.stop()
        segments = list(store.iter_segments())
        assert segments and all(s.display_name == "Alice" for s in segments)
        assert not timeline._pins
        assert all(entry.at >= 340000 for entry in timeline._entries)
    finally:
        release.set()
        pipeline.stop()
        store.close()


def test_controller_ingests_every_frame_and_keeps_gui_staleness_out_of_history(tmp_path):
    from gigaam_zoom.desktop import DesktopController, DesktopSettings
    ready, latest_called = threading.Event(), threading.Event()
    captures = []
    class Engine:
        def load(self):
            pass
        def transcribe_words(self, samples, rate):
            return [Word(.9, 1.2, "captured")]
    class Capture:
        metrics = {}
        running = False
        def __init__(self, **kwargs):
            self.audio = kwargs["on_audio"]
            captures.append(self)
        def start(self):
            self.running = True
        def stop(self):
            self.running = False
    class Observer:
        def subscribe(self, callback):
            self.callback = callback
        def start(self):
            self.origin = controller.store.get_metadata("origin_ms")
            for offset in (0, 750):
                self.callback(reading(self.origin + offset))
            self.callback(SpeakerObservation(self.origin + 1000, "local:zoom-ocr:unknown", None,
                                             "ocr_window_minimized"))
            for offset in (1750, 2500, 3250, 4000):
                self.callback(reading(self.origin + offset))
        def latest(self):
            latest_called.set()
            return SpeakerObservation(self.origin + 5000, "local:zoom-ocr:unknown", None, "ocr_stale")
        def stop(self):
            pass
    observer = Observer()
    controller = DesktopController(lambda kind, payload: ready.set() if kind == "ready" else None,
        engine_factory=lambda _: Engine(), capture_factory=Capture, observer_factory=lambda: observer)
    try:
        controller.start(DesktopSettings(model_dir=str(tmp_path), export_dir=str(tmp_path)), loopback_device=1)
        assert ready.wait(2)
        assert latest_called.wait(1)
        history = controller.pipeline.segmenter.timeline
        assert len(history._entries) == 7
        assert history._entries[-1].at == observer.origin + 4000
        assert all(entry.evidence != "ocr_stale" for entry in history._entries)
        captures[0].audio("remote", np.full(32000, 500, dtype="<i2").tobytes(), observer.origin, 16000)
        controller.stop()
        controller.thread.join(3)
        assert not controller.running and controller.failure is None
        segment = list(controller.store.iter_segments())[0]
        assert segment.display_name == "Неизвестно"
        assert segment.attribution_reason == "explicit_ocr_failure"
    finally:
        controller.stop()
        if controller.thread:
            controller.thread.join(3)
        if controller.store:
            controller.store.close()

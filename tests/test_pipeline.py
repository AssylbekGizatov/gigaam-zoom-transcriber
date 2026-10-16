"""Synthetic PCM and fake decoding: these tests make no ASR-quality claim."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

import numpy as np

from gigaam_zoom.models import AudioPacket, Word
from gigaam_zoom.pipeline import Pipeline
from gigaam_zoom.store import Store


def packet(user="42", name="Алия", ms=10000, seconds=.5, value=1000, epoch="connection-1"):
    return AudioPacket(user, name, ms, np.full(round(seconds * 16000), value, dtype="<i2").tobytes(), 16000, epoch)


class FakeEngine:
    def transcribe_words(self, samples, rate):
        if not np.any(samples):
            return []
        return [Word(.05, .2, "Сәлем"), Word(.21, .4, "world")]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "session.sqlite")
        self.events = []
        self.pipeline = None

    def tearDown(self):
        if self.pipeline:
            self.pipeline.stop()
        self.store.close()
        self.temp.cleanup()

    def make(self, engine=None, origin=10000, **kwargs):
        self.pipeline = Pipeline(self.store, engine or FakeEngine(),
                                 lambda kind, payload: self.events.append((kind, payload)), origin, **kwargs)
        self.pipeline.start()
        return self.pipeline

    def test_separate_source_ids_same_names_and_explicit_meeting_timeline(self):
        pipeline = self.make()
        self.assertTrue(pipeline.feed(packet(ms=11000)))
        self.assertTrue(pipeline.feed(packet(user="43", ms=12000)))
        pipeline.stop()
        segments = list(self.store.iter_segments())
        self.assertEqual([s.speaker_id for s in segments], ["42", "43"])
        self.assertEqual([s.display_name for s in segments], ["Алия", "Алия"])
        self.assertAlmostEqual(segments[0].start, 1.05)
        self.assertAlmostEqual(segments[1].end, 2.4)
        self.assertEqual(segments[0].source_epoch, "connection-1")
        self.assertEqual(self.store.get_metadata("time_basis"), "meeting")
        self.assertTrue(all(s.status == "final" and s.end > s.start for s in segments))

    def test_rename_leave_and_rejoin_snapshot_names_without_voice_guessing(self):
        pipeline = self.make()
        pipeline.feed(packet(name="Имя до", ms=10000))
        pipeline.participant_event("42", "Имя после", "rename", "connection-1", 10500)
        pipeline.feed(packet(name=None, ms=10500))
        pipeline.participant_event("42", None, "participant_left", "connection-1", 11000)
        pipeline.participant_event("42", "После входа", "participant_joined", "connection-2", 12000)
        pipeline.feed(packet(name=None, ms=12000, epoch="connection-2"))
        pipeline.stop()
        segments = list(self.store.iter_segments())
        self.assertEqual([s.display_name for s in segments], ["Имя до", "Имя после", "После входа"])
        self.assertEqual([s.source_epoch for s in segments], ["connection-1", "connection-1", "connection-2"])

    def test_reconnect_generation_does_not_inherit_old_name_or_deduplicate_other_source(self):
        pipeline = self.make()
        self.assertTrue(pipeline.feed(packet(name="Старое имя")))
        self.assertFalse(pipeline.feed(packet(name="Повтор")))
        self.assertTrue(pipeline.feed(packet(name=None, epoch="connection-2")))
        pipeline.stop()
        segments = list(self.store.iter_segments())
        self.assertEqual(len(segments), 2)
        self.assertEqual({s.source_epoch for s in segments}, {"connection-1", "connection-2"})
        self.assertTrue(any("имя неизвестно" in s.display_name for s in segments))

    def test_missing_origin_is_labelled_first_received_audio(self):
        pipeline = self.make(origin=None)
        pipeline.feed(packet(ms=1700000000000))
        pipeline.stop()
        self.assertEqual(self.store.get_metadata("origin_ms"), 1700000000000)
        self.assertEqual(self.store.get_metadata("time_basis"), "first_audio")
        self.assertAlmostEqual(list(self.store.iter_segments())[0].start, .05)

    def test_official_origin_must_precede_audio_and_cannot_rebase_finalized_data(self):
        pipeline = self.make(origin=None)
        pipeline.set_origin(9000)
        pipeline.feed(packet())
        with self.assertRaises(ValueError):
            pipeline.set_origin(8000)
        pipeline.stop()
        self.assertAlmostEqual(list(self.store.iter_segments())[0].start, 1.05)

    def test_silence_and_idle_flush_finish_partial_audio(self):
        pipeline = self.make(idle_timeout_seconds=.05)
        pipeline.feed(packet(value=0))
        pipeline.feed(packet(ms=10500))
        deadline = time.monotonic() + 2
        while not self.store.recent() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.store.recent(), "worker did not finalize on RTMS silence/inactivity")
        pipeline.feed(packet(ms=11000, seconds=1.3, value=0))
        pipeline.stop()
        self.assertEqual(len(self.store.recent()), 1)
        self.assertAlmostEqual(self.store.recent()[0].start, .55)

    def test_transport_gap_is_preserved_in_journal(self):
        pipeline = self.make()
        pipeline.feed(packet())
        pipeline.feed(packet(ms=12000))
        pipeline.stop()
        gaps = list(self.store.iter_gaps())
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0].start, gaps[0].end, gaps[0].reason), (.5, 2.0, "transport_gap"))
        self.assertEqual(len(self.store.recent()), 2)

    def test_bounded_global_audio_budget_includes_active_inference_overlap_and_all_sources(self):
        entered, release = threading.Event(), threading.Event()
        class SlowEngine:
            def transcribe_words(self, samples, rate):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("test release timeout")
                return []
        pipeline = self.make(SlowEngine())
        try:
            self.assertTrue(pipeline.feed(packet(seconds=8)))
            self.assertTrue(entered.wait(2))
            results = [pipeline.feed(packet(user=str(i), ms=10000, seconds=8)) for i in range(20)]
            self.assertIn(False, results)
            self.assertLessEqual(pipeline.pending_seconds, 48)
            self.assertLessEqual(pipeline.metrics["peak_pending_seconds"], 48)
            self.assertGreater(pipeline.metrics["dropped_seconds"], 0)
            self.assertTrue(any(g.reason == "queue_overload" for g in self.store.iter_gaps()))
        finally:
            release.set()
            pipeline.stop()
        self.assertEqual(pipeline.pending_seconds, 0)

    def test_hard_window_uses_overlap_ownership_without_duplicate_words(self):
        class OverlapEngine:
            calls = 0
            def transcribe_words(self, samples, rate):
                self.calls += 1
                if self.calls == 1:
                    return [Word(7.2, 7.4, "да"), Word(7.6, 7.8, "сейчас")]
                return [Word(.2, .4, "да"), Word(.6, .8, "сейчас"), Word(1.2, 1.4, "проверяем")]
        pipeline = self.make(OverlapEngine())
        pipeline.feed(packet(seconds=8))
        pipeline.feed(packet(ms=18000, seconds=2))
        pipeline.stop()
        self.assertEqual([s.text for s in self.store.iter_segments()], ["да", "сейчас проверяем"])
        self.assertEqual(pipeline.metrics["windows"], 2)

    def test_legitimate_repeated_phrase_after_flush_is_not_text_deduplicated(self):
        pipeline = self.make()
        pipeline.feed(packet())
        pipeline.flush()
        pipeline.feed(packet(ms=10500))
        pipeline.stop()
        self.assertEqual([s.text for s in self.store.iter_segments()], ["Сәлем world", "Сәлем world"])

    def test_invalid_model_times_and_exception_text_do_not_leak_and_next_window_continues(self):
        class BadThenGood(FakeEngine):
            calls = 0
            def transcribe_words(self, samples, rate):
                self.calls += 1
                if self.calls == 1:
                    return [Word(.2, .2, "invalid")]
                return super().transcribe_words(samples, rate)
        pipeline = self.make(BadThenGood())
        pipeline.feed(packet())
        pipeline.flush()
        pipeline.feed(packet(ms=10500))
        pipeline.stop()
        self.assertEqual(len(self.store.recent()), 1)
        self.assertEqual(list(self.store.iter_gaps())[0].reason, "inference_failed")
        self.assertTrue(any(kind == "inference_failed" for kind, _ in self.events))

    def test_invalid_packet_sizes_rates_and_missing_source_are_rejected(self):
        pipeline = self.make()
        invalid = [replace(packet(), pcm=b"x"), replace(packet(), sample_rate=1),
                   replace(packet(), user_id=""), replace(packet(), timestamp_ms=-1),
                   packet(seconds=8.1)]
        self.assertTrue(all(not pipeline.feed(p) for p in invalid))
        pipeline.stop()
        self.assertFalse(self.store.recent())

    def test_many_participant_incarnations_do_not_accumulate_in_memory(self):
        pipeline = self.make()
        for i in range(200):
            user = str(i)
            self.assertTrue(pipeline.feed(packet(user=user, value=0)))
            pipeline.participant_event(user, None, "left", "connection-1")
        self.assertEqual(pipeline.metrics["active_sources"], 0)
        self.assertEqual(pipeline.pending_seconds, 0)

    def test_full_disk_does_not_kill_worker_or_report_successful_drain(self):
        pipeline = self.make()
        def fail(segment):
            raise sqlite3.OperationalError("database or disk is full: private-path")
        self.store.upsert = fail
        pipeline.feed(packet())
        pipeline.flush()
        deadline = time.monotonic() + 2
        while not pipeline.failure and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(pipeline.failure)
        self.assertFalse(pipeline.feed(packet(ms=10500)))
        pipeline.stop()
        self.assertTrue(pipeline.metrics["failed"])
        self.assertGreater(pipeline.metrics["lost_seconds"], 0)
        self.assertTrue(any(kind == "store_error" for kind, _ in self.events))
        self.assertNotIn("private-path", str(self.events))
        self.assertFalse(self.store.recent())


if __name__ == "__main__":
    unittest.main()

"""Offline desktop integration: fake live capture, real Pipeline/SQLite/exports.

These deterministic tests never open microphones, access Zoom or establish
network connections. They do not claim real-meeting or model-quality coverage.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import numpy as np

from gigaam_zoom.desktop import DesktopController, DesktopSettings
from gigaam_zoom.desktop_attribution import DesktopSegmenter, Timeline
from gigaam_zoom.exports import EXPORT_FORMATS, export_transcript
from gigaam_zoom.models import AudioPacket, Segment, Word
from gigaam_zoom.pipeline import Pipeline
from gigaam_zoom.store import Store


class FakeEngine:
    def __init__(self):
        self.loaded = False
        self.load_entered = threading.Event()
        self.load_release = threading.Event()
        self.load_release.set()
        self.load_error = self.inference_error = None

    def load(self):
        self.load_entered.set()
        if not self.load_release.wait(5):
            raise RuntimeError("Test load timed out")
        if self.load_error:
            raise self.load_error
        self.loaded = True

    def transcribe_words(self, samples, rate):
        if self.inference_error:
            raise self.inference_error
        if not np.any(samples):
            return []
        return [Word(.05, .30, "Сәлем"), Word(.35, .65, "hello")]


class FakeCapture:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.running = False
        self.stopped = False
        self.metrics = {"test_capture": True, "audio_files_written": 0}

    def start(self):
        self.running = True

    def stop(self):
        self.stopped = True
        self.running = False

    def audio(self, source, timestamp_ms, seconds=1.0):
        pcm = np.full(round(16000 * seconds), 1000, dtype="<i2").tobytes()
        self.options["on_audio"](source, pcm, timestamp_ms, 16000)

    def fail(self):
        self.options["on_event"]("device_lost", {"message": "Тест: устройство отключено"})
        self.running = False


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.config = self.folder / "settings.json"
        self.settings = DesktopSettings(str(self.folder), str(self.folder), "Асылбек", True, False)

    def tearDown(self):
        self.temp.cleanup()

    def test_roundtrip_contains_only_public_desktop_settings(self):
        self.settings.save(self.config)
        self.assertEqual(DesktopSettings.load(self.config), self.settings)
        contents = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(contents, asdict(self.settings))
        self.assertFalse(self.config.with_suffix(".tmp").exists())
        for word in ("token", "secret", "client_id", "meeting_id", "passcode"):
            self.assertNotIn(word, contents)

    def test_rejects_secret_fields_mistyped_flags_and_invalid_json(self):
        for values in ({"access_token": "NEVER_EXPORT"}, {"client_secret": "NEVER_EXPORT"},
                       {"use_microphone": 1}, {"read_zoom_names": "false"},
                       {"model_dir": []}, []):
            with self.subTest(values=values):
                self.config.write_text(json.dumps(values), encoding="utf-8")
                with self.assertRaises(ValueError):
                    DesktopSettings.load(self.config)
        self.config.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            DesktopSettings.load(self.config)

    def test_migration_reads_only_public_paths_not_zoom_credentials(self):
        old = SimpleNamespace(model_dir=str(self.folder), export_dir=str(self.folder),
                              client_id="PRIVATE_CLIENT", client_secret="SECRET_TEST_VALUE")
        with patch("gigaam_zoom.config.Settings.load", return_value=old):
            migrated = DesktopSettings.load(self.config)
        migrated.save(self.config)
        contents = self.config.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_CLIENT", contents)
        self.assertNotIn("SECRET_TEST_VALUE", contents)
        self.assertEqual(migrated.model_dir, old.model_dir)
        self.assertEqual(migrated.export_dir, old.export_dir)

    def test_empty_missing_network_paths_and_empty_name_are_rejected(self):
        invalid = [replace(self.settings, model_dir=""),
                   replace(self.settings, export_dir=""),
                   replace(self.settings, model_dir=str(self.folder / "missing")),
                   replace(self.settings, export_dir=str(self.folder / "missing")),
                   replace(self.settings, export_dir="//example.invalid/share"),
                   replace(self.settings, local_name=" ")]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                value.validate()


class DesktopLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.settings = DesktopSettings(str(self.folder), str(self.folder), "Асылбек", False, False)
        self.engine = FakeEngine()
        self.events = []
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.created_capture = []

        def event(kind, payload):
            self.events.append((kind, payload))
            if kind == "ready":
                self.ready.set()
            elif kind == "finished":
                self.finished.set()

        def capture(**kwargs):
            result = FakeCapture(**kwargs)
            self.created_capture.append(result)
            return result

        self.controller = DesktopController(event, engine_factory=lambda _: self.engine,
                                            capture_factory=capture)

    def tearDown(self):
        self.engine.load_release.set()
        self.controller.stop()
        if self.controller.thread:
            self.controller.thread.join(5)
        if self.controller.store:
            self.controller.store.close()
        self.temp.cleanup()

    def start(self, settings=None):
        self.controller.start(settings or self.settings, loopback_device=12, microphone_device=13)
        self.assertTrue(self.ready.wait(3), self.events)
        self.assertTrue(self.controller.running)
        return self.created_capture[-1]

    def finish(self):
        self.controller.stop()
        self.controller.thread.join(5)
        self.assertFalse(self.controller.running)
        self.assertTrue(self.finished.is_set(), self.events)
        return [payload for kind, payload in self.events if kind == "finished"][-1]

    def test_offline_processing_preserves_capture_clock_and_exports_without_audio_files(self):
        # Any attempt to use a cloud STT, Zoom API or billing endpoint fails.
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), \
                patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Network forbidden")), \
                patch("socket.create_connection", side_effect=AssertionError("Network forbidden")):
            capture = self.start()
            origin = self.controller.store.get_metadata("origin_ms")
            capture.audio("remote", origin + 250)
            result = self.finish()
            self.assertFalse(result["failed"])
            segments = list(self.controller.store.iter_segments())
            self.assertEqual(len(segments), 1)
            segment = segments[0]
            self.assertAlmostEqual(segment.start, .30)
            self.assertAlmostEqual(segment.end, .90)
            self.assertEqual(segment.text, "Сәлем hello")
            self.assertEqual(segment.speaker_id, "local:remote")
            self.assertEqual(segment.attribution, "unattributed_mix")
            self.assertIsNone(self.controller.pipeline.transcriber)
            self.assertTrue(capture.stopped)
            for key in ("audio_saved", "video_saved", "cloud_stt"):
                self.assertIs(self.controller.store.get_metadata(key), False)
            self.assertEqual(self.controller.store.get_metadata("time_basis"), "local_capture")
            for kind in EXPORT_FORMATS:
                path = self.folder / ("transcript." + kind.lower())
                export_transcript(self.controller.store, path, kind)
                self.assertGreater(path.stat().st_size, 0)
            doc = json.loads((self.folder / "transcript.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["segments"][0]["attribution"], "unattributed_mix")
            self.assertEqual(doc["metadata"]["capture_mode"], "desktop_loopback")
        self.assertFalse(any(p.suffix.lower() in {".wav", ".mp3", ".m4a", ".mp4", ".pcm"}
                             for p in self.folder.rglob("*")))

    def test_microphone_opt_in_creates_separate_source_and_user_name(self):
        capture = self.start(replace(self.settings, use_microphone=True))
        self.assertEqual(capture.options["microphone_device"], 13)
        origin = self.controller.store.get_metadata("origin_ms")
        capture.audio("remote", origin + 100)
        capture.audio("microphone", origin + 200)
        self.finish()
        segments = list(self.controller.store.iter_segments())
        by_id = {s.speaker_id: s for s in segments}
        self.assertEqual(set(by_id), {"local:remote", "local:microphone"})
        self.assertEqual(by_id["local:microphone"].display_name, "Асылбек")
        self.assertEqual(by_id["local:microphone"].attribution, "local_microphone")

    def test_microphone_is_off_by_default_and_missing_selected_microphone_rejected(self):
        with self.assertRaises(ValueError):
            self.controller.start(replace(self.settings, use_microphone=True), 12, None)
        capture = self.start()
        self.assertIsNone(capture.options["microphone_device"])
        self.finish()

    def test_cancel_while_loading_never_opens_capture_or_creates_transcript(self):
        self.engine.load_release.clear()
        self.controller.start(self.settings, 12)
        self.assertTrue(self.engine.load_entered.wait(3))
        self.controller.stop()
        self.engine.load_release.set()
        result = self.finish()
        self.assertFalse(self.created_capture)
        self.assertIsNone(self.controller.store)
        self.assertFalse(self.ready.is_set())
        self.assertNotIn("Полный текст", result["message"])

    def test_model_load_failure_finishes_without_capture(self):
        self.engine.load_error = RuntimeError("Тест: модель недоступна")
        self.controller.start(self.settings, 12)
        self.controller.thread.join(3)
        self.assertTrue(self.finished.is_set(), self.events)
        self.assertFalse(self.created_capture)
        self.assertIsNone(self.controller.store)
        result = [p for kind, p in self.events if kind == "finished"][-1]
        self.assertTrue(result["failed"])
        self.assertIn("модель недоступна", result["message"])

    def test_capture_failure_drains_accepted_audio_and_marks_session_failed(self):
        capture = self.start()
        capture.audio("remote", self.controller.store.get_metadata("origin_ms") + 100)
        capture.fail()
        self.controller.thread.join(4)
        self.assertTrue(self.finished.is_set(), self.events)
        result = [p for kind, p in self.events if kind == "finished"][-1]
        self.assertTrue(result["failed"])
        self.assertIn("устройство отключено", result["message"])
        self.assertEqual(len(list(self.controller.store.iter_segments())), 1)
        self.assertIn("устройство отключено", self.controller.store.get_metadata("session_error"))
        self.assertTrue(capture.stopped)

    def test_silent_capture_shutdown_is_detected(self):
        capture = self.start()
        capture.running = False
        self.controller.thread.join(3)
        self.assertFalse(self.controller.running)
        self.assertTrue(self.finished.is_set(), self.events)
        result = [p for kind, p in self.events if kind == "finished"][-1]
        self.assertTrue(result["failed"])
        self.assertIn("Захват звука прекратился", result["message"])

    def test_double_start_rejected_stop_idempotent_restart_uses_new_store(self):
        capture = self.start()
        first_path = self.controller.store.path
        with self.assertRaises(ValueError):
            self.controller.start(self.settings, 12)
        self.finish()
        self.controller.stop()
        self.ready.clear()
        self.finished.clear()
        self.start()
        self.assertNotEqual(self.controller.store.path, first_path)
        self.assertTrue(first_path.exists())
        self.assertTrue(capture.stopped)
        self.finish()

    def test_failed_inference_does_not_claim_complete_transcript(self):
        self.engine.inference_error = RuntimeError("Тест: декодирование не удалось")
        capture = self.start()
        capture.audio("remote", self.controller.store.get_metadata("origin_ms") + 100)
        result = self.finish()
        self.assertEqual(len(list(self.controller.store.iter_gaps())), 1)
        self.assertNotIn("Полный текст", result["message"])
        self.assertTrue(result.get("failed") or result.get("partial"), result)

    def test_metadata_write_failure_still_emits_finished_and_releases_capture(self):
        capture = self.start()
        capture.audio("remote", self.controller.store.get_metadata("origin_ms") + 100)
        with patch.object(self.controller.store, "set_metadata", side_effect=sqlite3.OperationalError("disk full")):
            result = self.finish()
        self.assertTrue(result["failed"])
        self.assertTrue(capture.stopped)
        self.assertEqual(len(list(self.controller.store.iter_segments())), 1)

    def test_tail_capture_loss_is_partial_without_a_delivered_gap_event(self):
        capture = self.start()
        capture.metrics["dropped_seconds"] = .02
        capture.metrics["hardware_overflows"] = 1
        result = self.finish()
        self.assertTrue(result["partial"])
        self.assertIs(self.controller.store.get_metadata("partial_transcript"), True)


class AttributionPersistenceTests(unittest.TestCase):
    def test_whole_source_correction_updates_only_voice_id_in_selected_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "source-corrections.sqlite")
            try:
                originals = [
                    Segment("first", "local:voice:001", "Говорящий 1", 0, 1, "Первое", "final", "epoch-one", "voice_cluster"),
                    Segment("second", "local:voice:001", "Говорящий 1", 2, 3, "Второе", "final", "epoch-one", "voice_cluster"),
                    Segment("different-voice", "local:voice:002", "Говорящий 2", 4, 5, "Третье", "final", "epoch-one", "voice_cluster"),
                    Segment("restarted", "local:voice:001", "Говорящий 1", 6, 7, "Другой сеанс", "final", "epoch-two", "voice_cluster"),
                    Segment("mixed", "local:remote", "Не определён", 8, 9, "Смешанный", "final", "epoch-one", "unattributed_mix"),
                ]
                for segment in originals:
                    store.upsert(segment)
                store.correct_speaker("first", "Алия", whole_source=True)
                actual = {segment.id: segment for segment in store.iter_segments()}
                for segment in originals:
                    expected = (replace(segment, display_name="Алия", attribution="manual")
                                if segment.id in {"first", "second"} else segment)
                    self.assertEqual(actual[segment.id], expected)
                events = list(store.iter_participant_events())
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["source_epoch"], "epoch-one")
                self.assertEqual(events[0]["user_id"], "local:voice:001")
                self.assertEqual(events[0]["event"], "manual_source_correction:first")
            finally:
                store.close()

    def test_whole_source_correction_rejects_mixed_mic_ui_and_zoom_sources_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "restricted-corrections.sqlite")
            try:
                sources = [("local:remote", "unattributed_mix"),
                           ("local:microphone", "local_microphone"),
                           ("local:zoom-uia:one", "zoom_ui_hint"), ("42", "zoom_source_id")]
                for index, (source, attribution) in enumerate(sources):
                    segment = Segment(str(index), source, "Исходное имя", index, index + 1,
                                      "Сохранить текст", "final", "session", attribution)
                    store.upsert(segment)
                    with self.subTest(source=source), self.assertRaises(ValueError):
                        store.correct_speaker(segment.id, "Не применять ко всем", whole_source=True)
                    self.assertIn(segment, list(store.iter_segments()))
                self.assertEqual(list(store.iter_participant_events()), [])
                # A deliberate single-reply correction remains available.
                store.correct_speaker("0", "Ручная подпись одной реплики")
                self.assertEqual(store.recent()[0].attribution, "manual")
            finally:
                store.close()

    def test_finalization_persists_final_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "final-provenance.sqlite")
            try:
                preliminary = Segment("same", "local:voice:001", "Говорящий 1", 0, 1,
                                      "черновик", "preliminary", "session", "voice_cluster")
                final = replace(preliminary, text="финальный текст", status="final", end=2,
                                display_name="Алия", attribution="manual")
                store.upsert(preliminary)
                store.upsert(final)
                self.assertEqual(list(store.iter_segments()), [final])
            finally:
                store.close()

    def test_old_schema_migration_and_all_export_formats_explain_attribution(self):
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / "old.sqlite"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE segments (
                    id TEXT PRIMARY KEY, speaker_id TEXT NOT NULL, display_name TEXT NOT NULL,
                    start REAL NOT NULL, end REAL NOT NULL, text TEXT NOT NULL,
                    status TEXT NOT NULL, source_epoch TEXT NOT NULL);
                INSERT INTO segments VALUES ('old','42','Before',0,1,'Legacy','final','rtms');
            """)
            db.close()
            store = Store(path)
            try:
                self.assertEqual(store.recent()[0].attribution, "zoom_source_id")
                self.assertEqual(store.get_metadata("schema_version"), 2)
                store.set_metadata("capture_mode", "desktop_loopback")
                store.upsert(Segment("new", "local:zoom-uia:fixture", "Алия (по окну Zoom)",
                                     2, 3, "Қазақ Русский English", "final", "desktop", "zoom_ui_hint"))
                store.upsert(Segment("unknown", "local:remote", "Не определён", 4, 5,
                                     "Unknown", "final", "desktop", "unattributed_mix"))
                store.correct_speaker("unknown", "Исправлено вручную")
                self.assertEqual(store.recent()[-1].attribution, "manual")
                for kind in EXPORT_FORMATS:
                    destination = folder / ("export." + kind.lower())
                    export_transcript(store, destination, kind)
                    if kind == "DOCX":
                        with zipfile.ZipFile(destination) as archive:
                            text = archive.read("word/document.xml").decode("utf-8")
                    elif kind == "PDF":
                        text = "\n".join(page.extract_text() for page in PdfReader(destination).pages)
                    else:
                        text = destination.read_text(encoding="utf-8")
                    # Markdown escapes underscores; compare meaningful provenance
                    # after removing its escapes rather than implementation markup.
                    text = text.replace("\\_", "_")
                    for value in ("zoom_ui_hint", "manual", "по окну Zoom", "Исправлено вручную"):
                        self.assertIn(value, text, kind)
                    if kind != "JSON":
                        self.assertIn("это не Zoom ID", text, kind)
                doc = json.loads((folder / "export.json").read_text(encoding="utf-8"))
                self.assertEqual(doc["segments"][1]["attribution"], "zoom_ui_hint")
                self.assertEqual(doc["segments"][2]["attribution"], "manual")
            finally:
                store.close()


class OptionalAnalyzerTests(unittest.TestCase):
    def test_analyzer_error_keeps_asr_text_and_disables_only_failed_analyzer(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "analysis-failure.sqlite")
            events, calls = [], []
            def broken_analyzer(source_id, samples, rate, start):
                calls.append((source_id, len(samples), rate, start))
                raise RuntimeError("Optional speaker model failed")
            pipeline = Pipeline(store, FakeEngine(), lambda kind, payload: events.append((kind, payload)),
                                origin_ms=10_000, segmenter=DesktopSegmenter(Timeline()),
                                window_analyzer=broken_analyzer)
            try:
                pipeline.start()
                pcm = np.full(16_000, 1000, dtype="<i2").tobytes()
                pipeline.feed(AudioPacket("local:remote", "mixed", 10_500, pcm, epoch="capture"))
                pipeline.feed(AudioPacket("local:microphone", "me", 12_000, pcm, epoch="capture"))
                pipeline.stop()
                segments = list(store.iter_segments())
                self.assertEqual(len(segments), 2)
                self.assertEqual([s.text for s in segments], ["Сәлем hello", "Сәлем hello"])
                self.assertEqual({s.attribution for s in segments}, {"unattributed_mix", "local_microphone"})
                self.assertEqual(calls, [("local:remote", 16_000, 16_000, .5)])
                self.assertIsNone(pipeline.window_analyzer)
                self.assertEqual(sum(kind == "attribution_failed" for kind, _ in events), 1)
                self.assertFalse(any(kind in {"inference_failed", "store_error"} for kind, _ in events))
                self.assertEqual(list(store.iter_gaps()), [])
            finally:
                pipeline.stop()
                store.close()


if __name__ == "__main__":
    unittest.main()

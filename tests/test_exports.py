import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from gigaam_zoom.exports import EXPORT_FORMATS, export_transcript, format_time


class FakeStore:
    def __init__(self, count=3):
        self.count = count
        self.calls = 0

    def iter_segments(self):
        self.calls += 1
        for index in range(self.count):
            yield {"id": str(index), "speaker_id": f"zoom:{index % 2}",
                   "display_name": "Асылбек" if index % 2 else "Same [name]",
                   "start": index + 0.125, "end": index + 0.9,
                   "text": "Қазақ тілі Ә Ғ Қ Ң Ө Ұ Ү Һ І. Русский. English <xml> & **text**.",
                   "status": "interim" if index == 0 else "final", "source_epoch": "epoch1"}

    def get_metadata(self):
        return {"meeting_id": "public-test", "secret": "DO_NOT_EXPORT",
                "nested": {"access_token": "DO_NOT_EXPORT", "allowed": "yes"}}

    def iter_events(self):
        yield {"kind": "audio_gap", "start": 3, "end": 5,
               "reason": "reconnect", "authorization": "DO_NOT_EXPORT"}


class ExportTests(unittest.TestCase):
    def test_text_formats_and_json_preserve_ids_times_status_and_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("TXT", "MD", "JSON"):
                with self.subTest(kind=kind):
                    store = FakeStore()
                    path = export_transcript(store, Path(directory) / ("meeting." + kind.lower()), kind)
                    text = path.read_text(encoding="utf-8")
                    for value in ("Қазақ", "Русский", "English", "zoom:1", "epoch1", "interim", "final"):
                        self.assertIn(value, text)
                    self.assertIn(r"audio\_gap" if kind == "MD" else "audio_gap", text)
                    self.assertNotIn("DO_NOT_EXPORT", text)
                    self.assertEqual(store.calls, 1)
                    if kind == "JSON":
                        data = json.loads(text)
                        self.assertEqual(data["segments"][0]["start"], 0.125)
                        self.assertEqual(data["segments"][0]["end"], 0.9)
                        self.assertEqual(data["metadata"]["nested"], {"allowed": "yes"})
                        self.assertEqual(data["events"][0]["kind"], "audio_gap")
                    elif kind == "MD":
                        self.assertIn(r"\<xml\>", text)
                        self.assertIn(r"\[name\]", text)

    def test_never_overwrite_any_export_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing"
            path.write_bytes(b"original")
            for kind in EXPORT_FORMATS:
                with self.subTest(kind=kind), self.assertRaises(FileExistsError):
                    export_transcript(FakeStore(), path, kind)
                self.assertEqual(path.read_bytes(), b"original")

    def test_failed_export_removes_own_partial_file(self):
        class Broken(FakeStore):
            def iter_segments(self):
                yield next(super().iter_segments())
                raise RuntimeError("simulated disk/reader error")
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("TXT", "MD", "JSON"):
                path = Path(directory) / kind
                with self.assertRaises(RuntimeError):
                    export_transcript(Broken(), path, kind)
                self.assertFalse(path.exists())

    def test_invalid_times_are_not_silently_exported(self):
        class Broken(FakeStore):
            def iter_segments(self):
                row = next(super().iter_segments())
                row["end"] = float("nan")
                yield row
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            with self.assertRaises(ValueError):
                export_transcript(Broken(), path, "JSON")
            self.assertFalse(path.exists())

    def test_large_text_export_does_not_materialize_transcript(self):
        # Generator raises if treated as a sequence; one pass is required.
        class SinglePass(FakeStore):
            def iter_segments(self):
                if self.calls:
                    raise AssertionError("second transcript read")
                return super().iter_segments()
        with tempfile.TemporaryDirectory() as directory:
            store = SinglePass(20000)
            path = export_transcript(store, Path(directory) / "large.json", "JSON")
            self.assertGreater(path.stat().st_size, 1_000_000)
            self.assertEqual(store.calls, 1)

    def test_local_destinations_and_kind(self):
        for path in (r"\\server\share\meeting.txt", "https://example.com/test", "//server/share/test"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                export_transcript(FakeStore(), path, "TXT")
        with self.assertRaises(ValueError):
            export_transcript(FakeStore(), "unsupported", "HTML")
        with self.assertRaises(FileNotFoundError):
            export_transcript(FakeStore(), "missing-export-folder/test.txt", "TXT")

    def test_millisecond_rounding_and_long_meeting(self):
        self.assertEqual(format_time(59.9996), "00:01:00.000")
        self.assertEqual(format_time(360061.123), "100:01:01.123")

    def test_actual_store_gap_and_default_rtms_clock(self):
        from gigaam_zoom.store import Store
        from gigaam_zoom.models import Segment
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "public-test.sqlite3")
            store.append(Segment("1", "zoom-source", "Participant", 1, 2, "test"))
            store.gap("zoom-source", 2, 3, "reconnect", "epoch2")
            store.participant_event("zoom-source", "Participant", "join", "epoch2", 1000)
            store.participant_event("zoom-source", "Renamed", "rename", "epoch2", 2000)
            path = export_transcript(store, Path(directory) / "export.json", "JSON")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["timing"], "rtms_session_seconds")
            self.assertEqual(data["events"][0]["reason"], "reconnect")
            self.assertEqual(data["participants"][0]["speaker_id"], "zoom-source")
            participant_events = [event for event in data["events"] if event["kind"] == "participant_event"]
            self.assertEqual(len(participant_events), 2)
            self.assertEqual(participant_events[-1]["display_name"], "Renamed")
            store.set_metadata("time_basis", "meeting")
            path = export_transcript(store, Path(directory) / "meeting-time.json", "JSON")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["timing"], "meeting_seconds")
            store.set_metadata("time_basis", "first_audio")
            path = export_transcript(store, Path(directory) / "first-audio.json", "JSON")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["timing"], "first_audio_seconds")
            self.assertIn("первого полученного аудиопакета", data["note"])
            store.close()

    def test_docx_contains_unicode_and_ids(self):
        try:
            import docx  # noqa: F401
        except ImportError:
            self.skipTest("python-docx is not installed")
        with tempfile.TemporaryDirectory() as directory:
            path = export_transcript(FakeStore(), Path(directory) / "meeting.docx", "DOCX")
            with zipfile.ZipFile(path) as archive:
                xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("Қазақ тілі", xml)
            self.assertIn("zoom:1", xml)
            self.assertNotIn("DO_NOT_EXPORT", xml)

    def test_document_export_limits_clean_up_partial_files(self):
        try:
            import docx  # noqa: F401
            import reportlab  # noqa: F401
        except ImportError:
            self.skipTest("Document export dependencies unavailable")
        with tempfile.TemporaryDirectory() as directory:
            for kind, constant in (("DOCX", "MAX_DOCX_CHARACTERS"), ("PDF", "MAX_PDF_PAGES")):
                path = Path(directory) / ("bounded." + kind.lower())
                with patch("gigaam_zoom.exports." + constant, 1):
                    with self.assertRaises(ValueError):
                        export_transcript(FakeStore(30), path, kind)
                self.assertFalse(path.exists())

    def test_pdf_contains_embedded_unicode_font_and_multiple_pages(self):
        try:
            import reportlab  # noqa: F401
        except ImportError:
            self.skipTest("ReportLab is not installed")
        with tempfile.TemporaryDirectory() as directory:
            path = export_transcript(FakeStore(30), Path(directory) / "meeting.pdf", "PDF")
            payload = path.read_bytes()
            self.assertTrue(payload.startswith(b"%PDF-"))
            self.assertIn(b"DejaVuSans", payload)
            self.assertIn(b"/ToUnicode", payload)
            self.assertGreater(payload.count(b"/Type /Page\n"), 1)


if __name__ == "__main__":
    unittest.main()

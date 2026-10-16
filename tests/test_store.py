from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from gigaam_zoom.models import Segment
from gigaam_zoom.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Spaces, Unicode, URI-reserved characters are valid Windows filenames.
        self.path = Path(self.temp.name) / "Қазақ #1" / "meeting.sqlite"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def segment(self, id="one", start=1, status="final", text="Әлия: привет, world"):
        return Segment(id, "Zoom-42", "Әлия", start, start + .5, text, status, "epoch-1")

    def test_sorted_streaming_roundtrip_survives_reopen(self):
        self.store.append(self.segment("later", 20))
        self.store.append(self.segment("first", 0))
        self.store.set_metadata("origin_ms", 123456789)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual([s.id for s in self.store.iter_segments()], ["first", "later"])
        self.assertEqual(self.store.recent(1)[0].id, "later")
        self.assertEqual(self.store.get_metadata()["origin_ms"], 123456789)

    def test_preliminary_can_finalize_but_final_and_identity_are_immutable(self):
        partial = self.segment(status="preliminary", text="Әли")
        self.store.upsert(partial)
        with self.assertRaises(ValueError):
            self.store.upsert(replace(partial, speaker_id="another"))
        with self.assertRaises(ValueError):
            self.store.upsert(replace(partial, source_epoch="another"))
        final = replace(partial, text="Әлия", status="final")
        self.store.upsert(final)
        self.store.upsert(final)
        with self.assertRaises(ValueError):
            self.store.upsert(replace(final, text="modified"))
        self.assertEqual(self.store.recent(), [final])

    def test_names_and_ids_are_bound_parameters_not_sql(self):
        value = self.segment(id="'; DROP TABLE segments;--", text="ә ғ қ ң ө ұ ү һ і")
        self.store.upsert(value)
        self.assertEqual(list(self.store.iter_segments()), [value])
        self.store.participant_event("id'", "Алия", "join", "epoch-1")
        self.store.participant_event("id'", "Алия новая", "rename", "epoch-1")
        self.assertEqual(self.store.participant_name("id'", "epoch-1"), "Алия новая")
        self.assertIsNone(self.store.participant_name("id'", "epoch-2"))

    def test_gap_and_source_cursor_persist_without_audio(self):
        self.store.gap("42", 1, 2, "transport_gap", "e")
        self.store.set_source_cursor("42", "e", 5)
        self.store.set_source_cursor("42", "e", 4)
        self.assertEqual(self.store.source_cursor("42", "e"), 5)
        self.assertIsNone(self.store.source_cursor("42", "other"))
        self.assertEqual(list(self.store.iter_gaps())[0].speaker_id, "42")
        self.assertEqual(list(self.store.iter_gaps())[0].source_epoch, "e")

    def test_export_iterator_has_snapshot_while_background_worker_writes(self):
        for i in range(300):
            self.store.append(self.segment(str(i), i))
        iterator = self.store.iter_segments()
        first = next(iterator)
        errors = []
        def append():
            try:
                self.store.append(self.segment("new", 500))
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=append)
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        snapshot = [first, *iterator]
        self.assertEqual(len(snapshot), 300)
        self.assertEqual(len(self.store.recent(2000)), 301)

    def test_invalid_time_status_and_secret_metadata_are_rejected(self):
        for segment in [replace(self.segment(), start=float("nan")),
                        replace(self.segment(), end=1), replace(self.segment(), status="unknown")]:
            with self.assertRaises(ValueError):
                self.store.upsert(segment)
        for key in ("access_token", "client_secret", "password", "authorization"):
            with self.assertRaises(ValueError):
                self.store.set_metadata(key, "DO-NOT-PERSIST")
        self.assertNotIn(b"DO-NOT-PERSIST", self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()

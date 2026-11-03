"""Attribution contracts, using synthetic UI observations (not a Zoom test)."""
from dataclasses import dataclass
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from gigaam_zoom.desktop_attribution import DesktopSegmenter, Timeline, UNKNOWN
from gigaam_zoom.models import Word


@dataclass(frozen=True)
class Observation:
    observed_at_ms: int
    source_id: str = "local:zoom-uia:one"
    display_name: str | None = "Алия"
    evidence: str = "uia_explicit_active_speaker_hint"


class TimelineTests(unittest.TestCase):
    def test_requires_observation_before_entire_guard_interval(self):
        timeline = Timeline()
        timeline.add(Observation(1000))
        self.assertEqual(timeline.resolve(1299, 1400), UNKNOWN)
        self.assertEqual(timeline.resolve(1300, 1400).source_id, "local:zoom-uia:one")
        self.assertEqual(Timeline().resolve(1300, 1400), UNKNOWN)

    def test_strict_age_includes_end_guard_and_never_refreshes_on_read(self):
        timeline = Timeline()
        timeline.add(Observation(1000))
        self.assertNotEqual(timeline.resolve(2500, 2700), UNKNOWN)
        self.assertEqual(timeline.resolve(2500, 2701), UNKNOWN)
        self.assertEqual(timeline.resolve(5000, 5100), UNKNOWN)
        timeline.add(Observation(6000))
        # A recent observation does not repair a stale interval before it.
        self.assertEqual(timeline.resolve(5800, 6100), UNKNOWN)

    def test_transitions_on_both_sides_of_word_are_ambiguous(self):
        timeline = Timeline()
        timeline.add(Observation(1000))
        timeline.add(Observation(2000, "local:zoom-uia:two", "Дана"))
        self.assertNotEqual(timeline.resolve(1400, 1699), UNKNOWN)
        self.assertEqual(timeline.resolve(1400, 1700), UNKNOWN)
        self.assertEqual(timeline.resolve(2299, 2400), UNKNOWN)
        self.assertEqual(timeline.resolve(2300, 2400), UNKNOWN)
        self.assertEqual(timeline.resolve(2301, 2400).display_name, "Дана (по окну Zoom)")

    def test_same_name_is_not_same_source_and_rename_preserves_snapshot(self):
        timeline = Timeline()
        timeline.add(Observation(1000))
        timeline.add(Observation(2000, "local:zoom-uia:two"))
        timeline.add(Observation(3000, "local:zoom-uia:two", "Новое имя"))
        first = timeline.resolve(1400, 1600)
        second = timeline.resolve(2400, 2600)
        renamed = timeline.resolve(3400, 3600)
        self.assertNotEqual(first.source_id, second.source_id)
        self.assertEqual(first.display_name, second.display_name)
        self.assertEqual(second.source_id, renamed.source_id)
        self.assertNotEqual(second.display_name, renamed.display_name)
        self.assertEqual(timeline.resolve(2800, 2900), UNKNOWN)

    def test_missing_and_ambiguous_observations_break_old_name(self):
        for observation in [Observation(2000, display_name=None),
                            Observation(2000, evidence="ambiguous_active_speakers"),
                            Observation(2000, evidence="uia_unavailable"),
                            Observation(2000, evidence="uia_scan_incomplete"),
                            Observation(2000, evidence="unrecognized_success"),
                            Observation(2000, source_id="local:zoom-uiabogus"),
                            Observation(2000, source_id="local:zoom-uia:unknown"),
                            Observation(2000, source_id="42"),
                            Observation(2000, evidence="")]:
            with self.subTest(observation=observation):
                timeline = Timeline()
                timeline.add(Observation(1000))
                timeline.add(observation)
                self.assertEqual(timeline.resolve(2300, 2400), UNKNOWN)

    def test_metadata_gap_is_unknown_even_with_same_identity_at_both_ends(self):
        timeline = Timeline()
        timeline.add(Observation(1000))
        timeline.add(Observation(3500))
        self.assertEqual(timeline.resolve(1500, 3500), UNKNOWN)
        self.assertNotEqual(timeline.resolve(3800, 3900), UNKNOWN)

    def test_time_and_count_bounds(self):
        timeline = Timeline()
        for timestamp in range(1000):
            timeline.add(Observation(timestamp))
        self.assertEqual(len(timeline), 512)
        timeline.add(Observation(200_000))
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline.resolve(1000, 1100), UNKNOWN)

    def test_duplicate_timestamp_conflict_is_unknown_and_late_callbacks_are_ignored(self):
        timeline = Timeline()
        timeline.add(Observation(1000))
        timeline.add(Observation(1000))
        self.assertEqual(len(timeline), 1)
        timeline.add(Observation(1000, "local:zoom-uia:two"))
        self.assertEqual(timeline.resolve(1400, 1500), UNKNOWN)
        self.assertFalse(timeline.add(Observation(999)))
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline.resolve(1400, 1500), UNKNOWN)
        self.assertTrue(timeline.add(Observation(2000, evidence="uia_stale")))
        self.assertFalse(timeline.add(Observation(1900)))
        self.assertEqual(timeline.resolve(2400, 2500), UNKNOWN)

    def test_invalid_clock_and_intervals_are_rejected(self):
        timeline = Timeline()
        for invalid in [-1, True, 1.5]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                timeline.add(Observation(invalid))
        for start, end in [(-1, 3), (3, 2), (float("nan"), 4), (3, float("inf"))]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                timeline.resolve(start, end)


class DesktopSegmenterTests(unittest.TestCase):
    def test_microphone_is_separate_and_does_not_depend_on_zoom_metadata(self):
        segmenter = DesktopSegmenter(Timeline(), local_name="Асылбек")
        segments = segmenter("local:microphone", "capture-1", "Не использовать",
                             [Word(.1, .4, "Сәлем"), Word(.5, .8, "world")], 1000)
        self.assertEqual(len(segments), 1)
        segment = segments[0]
        self.assertEqual(segment.speaker_id, "local:microphone")
        self.assertEqual(segment.display_name, "Асылбек")
        self.assertEqual(segment.attribution, "local_microphone")
        self.assertEqual((segment.start, segment.end, segment.text), (.1, .8, "Сәлем world"))
        self.assertEqual(segment.source_epoch, "capture-1")

    def test_groups_words_by_hint_unknown_and_new_hint_with_relative_timestamps(self):
        timeline = Timeline()
        timeline.add(Observation(10_000))
        timeline.add(Observation(11_000))
        timeline.add(Observation(12_000, "local:zoom-uia:two", "Дана"))
        segmenter = DesktopSegmenter(timeline)
        segments = segmenter("local:remote", "capture-7", "Нельзя переносить это имя", [
            Word(.4, .6, "Первое"), Word(.7, .9, "слово"),
            Word(1.8, 1.9, "граница"), Word(2.4, 2.6, "Second"),
            Word(2.7, 2.8, "speaker"), Word(4.1, 4.3, "устарело"),
        ], 10_000)
        self.assertEqual([s.speaker_id for s in segments],
                         ["local:zoom-uia:one", "local:remote", "local:zoom-uia:two", "local:remote"])
        self.assertEqual([s.text for s in segments],
                         ["Первое слово", "граница", "Second speaker", "устарело"])
        self.assertEqual([s.start for s in segments], [.4, 1.8, 2.4, 4.1])
        self.assertEqual([s.end for s in segments], [.9, 1.9, 2.8, 4.3])
        self.assertEqual(segments[0].attribution, "zoom_ui_hint")
        self.assertEqual(segments[1].attribution, "unattributed_mix")
        self.assertTrue(all(s.source_epoch == "capture-7" and s.status == "final" for s in segments))
        self.assertEqual(len({s.id for s in segments}), len(segments))

    def test_unknown_source_never_takes_default_name_or_zoom_id(self):
        segmenter = DesktopSegmenter(Timeline())
        segment = segmenter("123", "capture", "Доверенное имя?", [Word(.1, .2, "text")], 1000)[0]
        self.assertEqual(segment.speaker_id, "local:remote")
        self.assertEqual(segment.display_name, UNKNOWN.display_name)
        self.assertEqual(segment.attribution, "unattributed_mix")

    def test_empty_and_invalid_words(self):
        segmenter = DesktopSegmenter(Timeline())
        self.assertEqual(segmenter("local:remote", "e", "", [], 1000), [])
        self.assertEqual(segmenter("local:remote", "e", "", [Word(.1, .2, " ")], 1000), [])
        for words in [[Word(.2, .1, "bad")], [Word(float("nan"), 1, "bad")],
                      [Word(.5, .8, "a"), Word(.2, .3, "b")]]:
            with self.subTest(words=words), self.assertRaises(ValueError):
                segmenter("local:remote", "e", "", words, 1000)
        with self.assertRaises(ValueError):
            DesktopSegmenter(Timeline(), " ")

    def test_optional_voice_clusters_are_only_an_unnamed_remote_fallback(self):
        diarization = Mock()
        diarization.resolve.return_value = SimpleNamespace(
            start=.1, end=1.0, local_speaker_id="local:voice:003", confidence=.75, reason="matched_cluster")
        segmenter = DesktopSegmenter(Timeline(), diarization=diarization)
        segment = segmenter("local:remote", "e", "Not a real name", [Word(.4, .6, "Hello")], 10_000)[0]
        diarization.resolve.assert_called_once_with(.4, .6)
        self.assertEqual(segment.speaker_id, "local:voice:003")
        self.assertEqual(segment.display_name, "Говорящий 3 (по голосу)")
        self.assertEqual(segment.attribution, "voice_cluster")
        diarization.resolve.reset_mock()
        microphone = segmenter("local:microphone", "e", "", [Word(.4, .6, "Local")], 10_000)[0]
        self.assertEqual(microphone.attribution, "local_microphone")
        diarization.resolve.assert_not_called()
        segmenter("unknown:source", "e", "", [Word(.4, .6, "Unknown")], 10_000)
        diarization.resolve.assert_not_called()

    def test_ui_hint_has_priority_over_optional_voice_cluster(self):
        timeline = Timeline()
        timeline.add(Observation(10_000))
        diarization = Mock()
        segmenter = DesktopSegmenter(timeline, diarization=diarization)
        segment = segmenter("local:remote", "e", "", [Word(.4, .6, "Hello")], 10_000)[0]
        self.assertEqual(segment.attribution, "zoom_ui_hint")
        diarization.resolve.assert_not_called()

    def test_voice_cluster_confidence_coverage_and_namespace_fail_closed(self):
        valid = dict(start=.1, end=1., local_speaker_id="local:voice:001", confidence=.60,
                     reason="matched_cluster")
        diarization = Mock()
        segmenter = DesktopSegmenter(Timeline(), diarization=diarization)
        for changes in ({"confidence": .5999}, {"confidence": float("nan")},
                        {"confidence": 1.1}, {"local_speaker_id": "Zoom:42"},
                        {"local_speaker_id": None}, {"local_speaker_id": "local:voice:000"},
                        {"start": .5}, {"end": .5}):
            with self.subTest(changes=changes):
                diarization.resolve.return_value = SimpleNamespace(**(valid | changes))
                result = segmenter("local:remote", "e", "", [Word(.4, .6, "Hi")], 10_000)[0]
                self.assertEqual(result.attribution, "unattributed_mix")
        diarization.resolve.return_value = SimpleNamespace(**valid)
        result = segmenter("local:remote", "e", "", [Word(.4, .6, "Hi")], 10_000)[0]
        self.assertEqual(result.attribution, "voice_cluster")
        diarization.resolve.return_value = None
        self.assertEqual(segmenter("local:remote", "e", "", [Word(.4, .6, "Hi")], 10_000)[0].attribution,
                         "unattributed_mix")
        diarization.resolve.side_effect = RuntimeError("Optional diarization unavailable")
        self.assertEqual(segmenter("local:remote", "e", "", [Word(.4, .6, "Hi")], 10_000)[0].attribution,
                         "unattributed_mix")


if __name__ == "__main__":
    unittest.main()

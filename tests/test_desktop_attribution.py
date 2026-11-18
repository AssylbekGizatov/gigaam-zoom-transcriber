import numpy as np
import pytest
from gigaam_zoom.desktop_attribution import DesktopSegmenter, Timeline, UNKNOWN
from gigaam_zoom.models import Word
from gigaam_zoom.speaker_observer import SpeakerObservation


def name(timeline, at, person):
    timeline.add(SpeakerObservation(at, "local:zoom-ocr:" + person, person, "ocr_speaker_view_hint"))


def test_visible_label_during_pause_does_not_carry_into_next_turn():
    timeline = Timeline()
    pcm = np.r_[np.full(16000, 300), np.zeros(16000), np.full(32000, 300)]
    timeline.add_audio("remote", 1000, pcm, 16000)
    for at in (1100, 1800, 2500, 3100, 3900):
        name(timeline, at, "Алия")
    assert timeline.resolve(1200, 1500).display_name == "Алия"
    assert timeline.resolve(2100, 2400) == UNKNOWN
    assert timeline.resolve(3000, 3060) == UNKNOWN
    assert timeline.resolve(3400, 3700).display_name == "Алия"
    assert timeline.resolve(4000, 4100) == UNKNOWN


def test_overlap_microphone_remains_local_remote_unconfirmed():
    timeline = Timeline()
    timeline.add_audio("remote", 1000, np.full(32000, 500), 16000)
    timeline.add_audio("microphone", 1500, np.full(6400, 500), 16000)
    for at in (1100, 2100):
        name(timeline, at, "Tom")
    segment = DesktopSegmenter(timeline, "Я")
    word = [Word(.55, .75, "да")]
    assert segment("local:microphone", "session", "", word, 1000)[0].display_name == "Я"
    assert segment("local:remote", "session", "", word, 1000)[0].display_name == "Неизвестно"


def test_long_unobserved_intervals_and_conflicts_are_unknown():
    timeline = Timeline()
    timeline.add_audio("remote", 1000, np.full(6 * 16000, 300), 16000)
    for at, person in ((1100, "Alex"), (2000, "Bob"), (5000, "Bob")):
        name(timeline, at, person)
    assert timeline.resolve(1300, 1500) == UNKNOWN
    assert timeline.resolve(2200, 3200) == UNKNOWN


def test_overlapping_word_intervals_are_unknown_even_with_stable_name():
    timeline = Timeline()
    timeline.add_audio("remote", 1000, np.full(48000, 300), 16000)
    for at in (1100, 1850, 2600):
        name(timeline, at, "Tom")
    segments = DesktopSegmenter(timeline)("local:remote", "session", "", [
        Word(.3, .6, "раз"), Word(.55, .7, "два")], 1000)
    assert all(s.display_name == "Неизвестно" for s in segments)

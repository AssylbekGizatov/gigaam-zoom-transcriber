"""OCR provenance through settings, timed attribution and persisted exports."""
from dataclasses import replace
import json

import pytest

from gigaam_zoom.desktop import DesktopSettings
from gigaam_zoom.desktop_attribution import DesktopSegmenter, Timeline, UNKNOWN
from gigaam_zoom.exports import export_transcript
from gigaam_zoom.models import Word
from gigaam_zoom.store import Store
from gigaam_zoom.zoom_accessibility import SpeakerObservation


def observation(at, name="Алия", source="a", evidence="ocr_speaker_view_hint"):
    return SpeakerObservation(at, "local:zoom-ocr:" + source, name, evidence)


def test_ocr_names_follow_audio_clock_not_transcription_completion(tmp_path):
    timeline = Timeline()
    timeline.add(observation(10000))
    timeline.add(observation(10750))
    timeline.add(observation(11500, None, "unknown", "ocr_confirming"))
    timeline.add(observation(12250, "Bob", "b"))
    timeline.add(observation(13000, "Bob", "b"))
    timeline.add(observation(13750, None, "unknown", "ocr_not_foreground"))
    segmenter = DesktopSegmenter(timeline)
    segments = segmenter("local:remote", "session", "Do not use", [
        Word(.4, .7, "Первая"), Word(1.4, 1.6, "переход"),
        Word(2.6, 2.8, "Second"), Word(4.1, 4.3, "unknown"),
    ], 10000)
    assert [s.attribution for s in segments] == [
        "zoom_ocr_hint", "unattributed_mix", "zoom_ocr_hint", "unattributed_mix"]
    assert segments[0].display_name == "Алия (OCR окна Zoom)"
    assert segments[2].display_name == "Bob (OCR окна Zoom)"
    store = Store(tmp_path / "session.sqlite")
    try:
        store.set_metadata("capture_mode", "desktop_loopback")
        for segment in segments:
            store.upsert(segment)
        export_transcript(store, tmp_path / "session.json", "JSON")
        exported = json.loads((tmp_path / "session.json").read_text(encoding="utf-8"))
        assert exported["segments"][0]["attribution"] == "zoom_ocr_hint"
        assert exported["segments"][2]["speaker_id"] != exported["segments"][0]["speaker_id"]
    finally:
        store.close()


@pytest.mark.parametrize("source,evidence", [
    ("local:zoom-uia:a", "ocr_speaker_view_hint"),
    ("local:zoom-ocr:a", "uia_explicit_active_speaker_hint"),
    ("local:zoom-ocr:unknown", "ocr_speaker_view_hint"),
    ("local:zoom-ocr:a", "ocr_confirming"),
    ("local:zoom-ocr:a", "ocr_unavailable"),
])
def test_failure_and_mismatched_evidence_never_become_names(source, evidence):
    timeline = Timeline()
    timeline.add(SpeakerObservation(1000, source, "Alice", evidence))
    assert timeline.resolve(1400, 1600) == UNKNOWN


def test_ocr_guard_covers_switch_between_two_capture_samples():
    timeline = Timeline(guard_ms=1000)
    for at in (0, 500, 1250):
        timeline.add(observation(at))
    # A speaks at 1250, B starts at 1300 and is first seen at 2000.
    timeline.add(observation(2000, None, "unknown", "ocr_confirming"))
    assert timeline.resolve(1400, 1500) == UNKNOWN
    for at in (2750, 3500, 4250, 5000):
        timeline.add(observation(at, "Bob", "b"))
    assert timeline.resolve(3900, 4000).display_name == "Bob (OCR окна Zoom)"


def test_ocr_settings_roundtrip_and_legacy_defaults(tmp_path):
    settings = DesktopSettings(model_dir=str(tmp_path), export_dir=str(tmp_path),
        zoom_name_mode="ocr", zoom_ocr_region="0.1,0.8,0.4,0.1", zoom_ocr_language="ru-RU",
        zoom_ocr_client_size="1280,720")
    path = tmp_path / "settings.json"
    settings.save(path)
    assert DesktopSettings.load(path) == settings
    assert settings.visual_config().region == (.1, .8, .4, .1)
    assert settings.visual_config().client_size == (1280, 720)
    path.write_text(json.dumps({"model_dir": str(tmp_path), "export_dir": str(tmp_path)}))
    assert DesktopSettings.load(path).zoom_name_mode == "uia"
    for region in ("", "NaN,0,1,1", "0,0,-1,1", "0,0,1.1,1", "0,1,2"):
        with pytest.raises(ValueError):
            replace(settings, zoom_ocr_region=region).validate()
    with pytest.raises(ValueError):
        replace(settings, zoom_name_mode="guess").validate()


def _ocr_process_fixture(connection, session_id, config):
    from gigaam_zoom.zoom_accessibility import monotonic_ms
    try:
        while connection.recv() == "sample":
            connection.send(observation(monotonic_ms(), config.language))
    except EOFError:
        pass
    finally:
        connection.close()


def test_observer_passes_visual_config_to_spawned_worker_and_clears_on_stop():
    import time
    from gigaam_zoom.zoom_accessibility import ZoomSpeakerObserver
    from gigaam_zoom.zoom_visual import VisualSpeakerConfig
    observer = ZoomSpeakerObserver(visual_config=VisualSpeakerConfig((0, .8, .5, .1)),
                                   _worker=_ocr_process_fixture, probe_timeout=4)
    assert observer.latest().evidence == "ocr_not_started"
    observer.start()
    try:
        deadline = time.monotonic() + 6
        while observer.latest().display_name is None and time.monotonic() < deadline:
            time.sleep(.02)
        result = observer.latest()
        assert result.display_name == "ru-RU"
        assert observer.latest(result.observed_at_ms + 2001).evidence == "ocr_stale"
    finally:
        observer.stop()
    assert not observer._thread.is_alive()
    assert observer.latest().evidence == "ocr_stopped"

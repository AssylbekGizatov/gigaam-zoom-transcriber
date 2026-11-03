import numpy as np
import pytest

from gigaam_zoom.diarization import (
    DiarizationError, DiarizationTimeline, OnlineDiarizer, SAMPLE_RATE, SpeakerSpan,
    SPEAKER_MODELS, verify_model,
)


def sound(seconds=2, sign=1):
    # Distinct deterministic vectors without relying on a downloaded model.
    return (sign * 0.2 * np.tile([1, -0.8], round(seconds * SAMPLE_RATE / 2))).astype(np.float32)


def make_diarizer(embedder, **kwargs):
    return OnlineDiarizer(embedder, window_seconds=2, match_threshold=.73,
                         new_threshold=.68, margin=.06, half_similarity=.60, **kwargs)


class Embedder:
    def __init__(self):
        self.calls = 0
    def __call__(self, samples):
        self.calls += 1
        return [1, 0, 0] if samples[0] > 0 else [0, 1, 0]


def test_voice_clusters_return_without_names_and_reuse():
    d = make_diarizer(Embedder())
    first = d.analyze(sound(3), 0)
    second = d.analyze(sound(3, -1), 5)
    again = d.analyze(sound(3), 10)
    assert first[0].local_speaker_id == "local:voice:001"
    assert second[-1].local_speaker_id == "local:voice:002"
    assert again[-1].local_speaker_id == "local:voice:001"
    assert first[0].display_name == "Говорящий 1 (по голосу)"
    assert d.speaker_count == 2
    assert d.buffered_samples < 2 * SAMPLE_RATE


def test_quiet_clipping_short_and_invalid_audio_never_enroll():
    model = Embedder()
    d = make_diarizer(model)
    assert d.analyze(np.zeros(32000, np.float32), 0)[0].local_speaker_id is None
    assert d.analyze(np.ones(32000, np.float32), 2)[0].local_speaker_id is None
    assert d.speaker_count == 0
    assert model.calls == 0
    assert all(s.local_speaker_id is None for s in d.flush())
    assert d.analyze(sound(.5), 6) == []
    assert d.flush()[0].reason == "short_window"
    for values in (np.array([float("nan")]), np.ones(10) * 12, np.zeros((2, 2))):
        with pytest.raises(ValueError):
            d.analyze(values, 10)


def test_mixed_new_window_is_not_enrolled():
    d = make_diarizer(Embedder())
    mixed = np.concatenate((sound(1), sound(1, -1)))
    result = d.analyze(mixed, 0)[0]
    assert result.reason == "unstable_or_mixed_voice"
    assert result.local_speaker_id is None
    assert d.speaker_count == 0


def test_speaker_limit_and_uncertain_cosine():
    d = make_diarizer(Embedder(), max_speakers=1)
    d.analyze(sound(2), 0)
    assert d.analyze(sound(2, -1), 4)[-1].reason == "speaker_limit"
    d.embedder = lambda samples: [0.70, 0.7141428, 0]
    assert d.analyze(sound(2), 8)[-1].reason == "ambiguous_voice"
    assert d.speaker_count == 1


def test_bad_embeddings_and_retrograde_time_are_rejected():
    d = make_diarizer(lambda samples: [float("nan"), 1])
    with pytest.raises(DiarizationError):
        d.analyze(sound(2), 0)
    d = make_diarizer(Embedder())
    d.analyze(sound(2), 5)
    with pytest.raises(ValueError):
        d.analyze(sound(2), 4)


def test_overlapping_asr_windows_are_processed_once():
    model = Embedder()
    timeline = DiarizationTimeline(make_diarizer(model))
    timeline.analyze_window("local:remote", sound(8), SAMPLE_RATE, 0)
    assert len(timeline._spans) == 7
    timeline.analyze_window("local:remote", sound(8), SAMPLE_RATE, 6)
    assert len(timeline._spans) == 13
    assert timeline._seen_end == 14
    calls = model.calls
    timeline.analyze_window("local:remote", sound(8), SAMPLE_RATE, 6)
    timeline.analyze_window("local:microphone", sound(8), SAMPLE_RATE, 14)
    assert model.calls == calls
    span = timeline.resolve(1.4, 2.4)
    assert span.local_speaker_id == "local:voice:001"
    assert timeline.resolve(13.4, 13.9) is None  # unfinished context


def test_timeline_does_not_bridge_gap_or_speaker_change():
    timeline = DiarizationTimeline(make_diarizer(Embedder()))
    timeline.analyze_window("local:remote", sound(3), SAMPLE_RATE, 0)
    timeline.analyze_window("local:remote", sound(3, -1), SAMPLE_RATE, 5)
    assert timeline.resolve(1.8, 5.2) is None
    assert timeline.resolve(2.2, 2.3) is None
    assert timeline.resolve(5.2, 5.3).local_speaker_id == "local:voice:002"
    timeline._spans.clear()
    timeline._spans.extend([
        SpeakerSpan(0, 1, "local:voice:001", .9, "test"),
        SpeakerSpan(1, 2, "local:voice:002", .9, "test"),
    ])
    assert timeline.resolve(.5, 1.5) is None


def test_recent_timeline_and_reset_remain_bounded():
    timeline = DiarizationTimeline(make_diarizer(Embedder()), retention_seconds=5, max_spans=8)
    for start in range(0, 90, 3):
        timeline.analyze_window("local:remote", sound(3), SAMPLE_RATE, start)
    assert len(timeline._spans) <= 8
    assert timeline._spans[0].end >= 85
    assert timeline.diarizer.buffered_samples < 32000
    timeline.reset()
    assert not timeline._spans
    assert timeline.diarizer.speaker_count == 0
    assert timeline.diarizer.buffered_samples == 0


def test_missing_or_untrusted_model_is_rejected_without_import(tmp_path):
    path = tmp_path / "model.onnx"
    with pytest.raises(DiarizationError):
        verify_model(path)
    path.write_bytes(b"not a model")
    with pytest.raises(DiarizationError):
        verify_model(path)


def test_default_three_second_window_and_one_second_step():
    d = OnlineDiarizer(Embedder())
    assert d.analyze(sound(2), 0) == []
    first = d.analyze(sound(1), 2)
    assert [(span.start, span.end) for span in first] == [(0, 1)]
    assert d.buffered_samples == 2 * SAMPLE_RATE
    next_spans = d.analyze(sound(1), 3)
    assert [(span.start, span.end) for span in next_spans] == [(1, 2)]


def test_each_verified_model_has_its_own_threshold_profile():
    default = OnlineDiarizer(Embedder())
    assert (default.match_threshold, default.new_threshold) == (.60, .35)
    embedder = Embedder()
    embedder.model_spec = SPEAKER_MODELS["wespeaker"]
    historical = OnlineDiarizer(embedder)
    assert (historical.match_threshold, historical.new_threshold) == (.73, .68)
    assert SPEAKER_MODELS["campplus"].license == "Apache-2.0"
    assert SPEAKER_MODELS["wespeaker"].license == "CC-BY-4.0"

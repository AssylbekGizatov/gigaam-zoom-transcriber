"""A derived name is reversible. The underlying capture row remains idempotent."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from itertools import permutations

import pytest

from gigaam_zoom.models import Segment
from gigaam_zoom.store import Store


A = Segment("left", "local:zoom-ocr:a", "Алия", 0, 1, "один", "final", "session",
            "zoom_ocr_hint", "stable_ocr_bracket")
GAP = Segment("between", "local:remote", "Неизвестно", 1.2, 2.2, "два", "final", "session",
              "unattributed_mix", "no_observations")
C = replace(A, id="right", start=2.6, end=3, text="три")


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "test.sqlite")
    try:
        yield db
    finally:
        db.close()


def selected(store):
    return next(segment for segment in store.iter_segments() if segment.id == GAP.id)


@pytest.mark.parametrize("order", list(permutations([A, GAP, C])))
def test_inferred_provenance_survives_reopen_and_raw_delivery_replay(store, order):
    for segment in order:
        store.upsert(segment)
    store.upsert(GAP)
    other = Store(store.path)
    try:
        expected = replace(GAP, speaker_id=A.speaker_id, display_name=A.display_name,
                           attribution="inferred_gap_fill")
        assert selected(other) == expected == other.recent()[1] == other.segment_by_id(GAP.id)
        assert other.segment_by_id("unknown-id") is None
    finally:
        other.close()


@pytest.mark.parametrize("left,mid,right", [
    (A, replace(GAP, end=2.7), replace(C, start=2.8)),          # exactly 1.5 s
    (A, replace(GAP, start=3, end=3.3), replace(C, start=3.6, end=4)),  # exactly 2 s before
    (A, GAP, replace(C, start=4.2, end=5)),                   # exactly 2 s after
    (A, GAP, replace(C, display_name="Ренат")),
    (A, GAP, replace(C, speaker_id="local:zoom-ocr:different")),
    (A, GAP, replace(C, source_epoch="other")),
    (A, GAP, replace(C, status="preliminary")),
    (replace(A, attribution="manual"), GAP, C),
    (replace(A, attribution="inferred_gap_fill"), GAP, C),
    (A, replace(GAP, speaker_id="local:microphone"), C),
    (A, replace(GAP, attribution_reason=""), C),               # legacy unknown
    (A, replace(GAP, attribution_reason="speaker_conflict"), C),
    (A, replace(GAP, attribution_reason="audio_silence"), C),
    (A, replace(GAP, attribution_reason="microphone_overlap"), C),
    (A, replace(GAP, attribution_reason="explicit_ocr_failure"), C),
])
def test_boundaries_and_unconfirmed_anchors_keep_unknown(store, left, mid, right):
    for segment in (left, mid, right):
        store.upsert(segment)
    assert selected(store) == mid


@pytest.mark.parametrize("kind", ["overlap", "mic", "gap", "second_unknown", "tied_anchor"])
def test_late_conflict_revokes_existing_inference(store, kind):
    for segment in (A, GAP, C):
        store.upsert(segment)
    assert selected(store).attribution == "inferred_gap_fill"
    if kind == "overlap":
        store.upsert(replace(C, id="other", start=.95, end=2.8, display_name="Ренат"))
    elif kind == "mic":
        store.upsert(replace(C, id="mic", start=2.25, end=2.45, speaker_id="local:microphone",
                             display_name="Вы", attribution="local_microphone"))
    elif kind == "gap":
        store.gap("local:remote", 1.1, 1.15, "transport_gap", "session")
    elif kind == "second_unknown":
        store.upsert(replace(GAP, id="second_unknown", start=2.25, end=2.5))
    else:
        store.upsert(replace(A, id="also_left", start=.5, display_name="Ренат"))
    assert selected(store) == GAP


def test_manual_override_and_edited_anchors_take_priority(store):
    for segment in (A, GAP, C):
        store.upsert(segment)
    store.correct_speaker(C.id, "Исправленный сосед")
    assert selected(store) == GAP
    store.correct_speaker(GAP.id, "Ручное имя")
    store.upsert(GAP)  # delayed capture replay must not revert a correction
    corrected = selected(store)
    assert (corrected.display_name, corrected.attribution) == ("Ручное имя", "manual")
    assert (corrected.start, corrected.end, corrected.text) == (GAP.start, GAP.end, GAP.text)
    assert [r["event"] for r in store.iter_participant_events()] == ["manual_correction:right", "manual_correction:between"]


def test_concurrent_insert_and_manual_correction_cannot_restore_inferred_name(store):
    for segment in (A, GAP):
        store.upsert(segment)
    second = Store(store.path)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            insert = pool.submit(second.upsert, C)
            correct = pool.submit(store.correct_speaker, GAP.id, "Исправлено")
            insert.result()
            correct.result()
        assert selected(store).display_name == "Исправлено"
        assert selected(second).attribution == "manual"
    finally:
        second.close()


def test_changes_are_logged_after_commit_without_names_or_text(store, monkeypatch):
    from gigaam_zoom import store as module
    observer = Store(store.path)
    events = []
    def log(event, **fields):
        # Independent reader can already see the new identity when logged.
        events.append((event, selected(observer).attribution, fields))
    monkeypatch.setattr(module, "log_event", log)
    try:
        for segment in (A, GAP, C, C):
            store.upsert(segment)
        store.correct_speaker(GAP.id, "Ручная правка")
        expected = {"segment_id": GAP.id, "left_anchor_id": A.id, "right_anchor_id": C.id}
        assert events == [("gap_fill_applied", "inferred_gap_fill", expected),
                          ("gap_fill_withdrawn", "manual", expected)]
    finally:
        observer.close()


@pytest.mark.parametrize("evidence,can_fill", [
    ("ocr_name_missing", True), ("ocr_region_occluded", True), ("ocr_window_minimized", False),
])
def test_capture_words_keep_original_reason_and_only_fill_temporary_absence(store, evidence, can_fill):
    import numpy as np
    from gigaam_zoom.desktop_attribution import DesktopSegmenter, Timeline
    from gigaam_zoom.models import Word
    from gigaam_zoom.speaker_observer import SpeakerObservation
    timeline = Timeline()
    origin = 10_000
    timeline.add_audio("remote", origin, np.full(4 * 16000, 300, dtype=np.int16), 16000)
    for ms in (0, 800, 1500, 2500, 3100):
        if ms == 1500:
            timeline.add(SpeakerObservation(origin + ms, "local:zoom-ocr:unknown", None, evidence))
        else:
            timeline.add(SpeakerObservation(origin + ms, A.speaker_id, A.display_name, "ocr_speaker_view_hint"))
    words = [Word(.3, .5, "раз"), Word(1.4, 1.6, "два"), Word(2.6, 2.9, "три")]
    segments = DesktopSegmenter(timeline)("local:remote", "session", "", words, origin)
    assert [row.attribution for row in segments] == ["zoom_ocr_hint", "unattributed_mix", "zoom_ocr_hint"]
    for row in segments:
        store.upsert(row)
    result = store.segment_by_id(segments[1].id)
    if can_fill:
        assert result.attribution == "inferred_gap_fill" and result.display_name == A.display_name
        assert result.attribution_reason == "temporary_ocr_absence"
    else:
        assert result == segments[1]

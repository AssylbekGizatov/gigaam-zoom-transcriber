import time

import pytest

from gigaam_zoom import zoom_accessibility as module

from gigaam_zoom.zoom_accessibility import (
    AccessibleLabel, SpeakerObservation, UNKNOWN_SOURCE_ID, ZoomSpeakerObserver,
    monotonic_ms, observation_from_labels, parse_active_speaker_label,
)


@pytest.mark.parametrize("label,name", [
    ("Active speaker: Асқар", "Асқар"), ("Speaking: Alice", "Alice"),
    ("Alice is speaking", "Alice"), ("Говорит: Иван", "Иван"),
    ("Сейчас говорит: Әлия", "Әлия"), ("Активный говорящий: Иван", "Иван"),
])
def test_explicit_speaker_labels(label, name):
    assert parse_active_speaker_label(label) == name


@pytest.mark.parametrize("label", [
    "Alice", "Alice (Host)", "Mute Alice", "Pin active speaker", "Speaker view",
    "Active speaker:", "Active speaker: …", "Active speaker: Alice\nMute",
    "Participants: Alice, Bob", "Speaking", "Active speaker: " + "A" * 161,
    "Активный участник: Иван",
    "Говорит: Иван\x00", "Active speaker: Alice\u202e",
])
def test_no_inference_from_names_controls_or_compound_labels(label):
    assert parse_active_speaker_label(label) is None


def test_duplicate_names_and_multiple_sources_are_unknown():
    labels = [AccessibleLabel("ui-1", "Active speaker: Alex"),
              AccessibleLabel("ui-2", "Active speaker: Alex")]
    result = observation_from_labels(labels, 100)
    assert result.display_name is None
    assert result.evidence == "uia_ambiguous"
    assert result.source_id == UNKNOWN_SOURCE_ID


def test_rename_preserves_local_ui_identity_rejoin_changes_it():
    before = observation_from_labels([AccessibleLabel("window1:node1", "Speaking: Alex")], 100)
    renamed = observation_from_labels([AccessibleLabel("window1:node1", "Speaking: Әлия")], 200)
    rejoined = observation_from_labels([AccessibleLabel("window1:node2", "Speaking: Әлия")], 300)
    assert before.source_id == renamed.source_id
    assert renamed.source_id != rejoined.source_id
    assert renamed.source_id.startswith("local:zoom-uia:")
    assert renamed.display_name == "Әлия"


def test_incomplete_scan_and_conflicting_same_node_fail_closed():
    labels = [AccessibleLabel("node", "Speaking: Alice")]
    assert observation_from_labels(labels, 100, complete=False).display_name is None
    labels.append(AccessibleLabel("node", "Speaking: Bob"))
    assert observation_from_labels(labels, 100).evidence == "uia_ambiguous"
    assert observation_from_labels([], 100).evidence == "uia_missing"


def test_same_node_repeat_deduplicates():
    label = AccessibleLabel("node", "Speaking: Alice")
    assert observation_from_labels([label, label], 100).display_name == "Alice"


def test_stale_and_clock_regression_drop_name():
    observer = ZoomSpeakerObserver(max_age_ms=1000)
    observer._publish(SpeakerObservation(5000, "local:test", "Alice", "test"))
    assert observer.latest(6000).display_name == "Alice"
    assert observer.latest(6001).evidence == "uia_stale"
    assert observer.latest(4999).display_name is None
    observer.stop()
    assert observer.latest().evidence == "uia_stopped"


def _test_worker(connection, session_id):
    try:
        while connection.recv() == "sample":
            connection.send(SpeakerObservation(monotonic_ms(), "local:test", "Alice", "test"))
    except EOFError:
        pass
    finally:
        connection.close()


def _blocked_worker(connection, session_id):
    time.sleep(20)


def _wait_until(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("observer did not reach expected state")


def test_spawned_worker_lifecycle_and_nonblocking_latest():
    observer = ZoomSpeakerObserver(_worker=_test_worker, probe_timeout=4)
    observer.start()
    thread = observer._thread
    observer.start()
    assert observer._thread is thread
    try:
        _wait_until(lambda: observer.latest().display_name == "Alice")
        started = time.monotonic()
        for _ in range(100):
            observer.latest()
        assert time.monotonic() - started < 0.1
    finally:
        observer.stop()
    assert not thread.is_alive()
    assert observer.latest().display_name is None


def test_hung_com_provider_is_timed_out_and_shutdown_is_bounded():
    observer = ZoomSpeakerObserver(_worker=_blocked_worker, probe_timeout=0.2)
    observer.start()
    try:
        _wait_until(lambda: observer.latest().evidence == "uia_timeout")
        started = time.monotonic()
        observer.stop()
        assert time.monotonic() - started < 2
        assert not observer._thread.is_alive()
    finally:
        observer.stop()


def test_poll_rate_is_bounded():
    with pytest.raises(ValueError):
        ZoomSpeakerObserver(poll_interval=0.1)


class _Element:
    def __init__(self, name="", pid=5, offscreen=False):
        self.CurrentName = name
        self.CurrentProcessId = pid
        self.CurrentIsOffscreen = offscreen
        self.children = []
        self.sibling = None

    def GetRuntimeId(self):
        return (id(self),)

    def add(self, child):
        if self.children:
            self.children[-1].sibling = child
        self.children.append(child)
        return child


class _Automation:
    def __init__(self, root):
        self.root = root
        self.ControlViewWalker = self

    def ElementFromHandle(self, hwnd):
        return self.root

    def GetFirstChildElement(self, element):
        return element.children[0] if element.children else None

    def GetNextSiblingElement(self, element):
        return element.sibling


def test_native_tree_adapter_filters_foreign_and_hidden_elements(monkeypatch):
    monkeypatch.setattr(module, "_zoom_windows", lambda: [(100, 5)])
    root = _Element()
    root.add(_Element("Speaking: Hidden", offscreen=True))
    root.add(_Element("Speaking: Foreign", pid=6))
    root.add(_Element("Speaking: Alice"))
    result = module._scan_uia(_Automation(root), "test-session")
    assert result.display_name == "Alice"


def test_depth_budget_never_returns_early_candidate_as_certain(monkeypatch):
    monkeypatch.setattr(module, "_zoom_windows", lambda: [(100, 5)])
    root = _Element("Speaking: Alice")
    node = root
    for _ in range(14):
        node = node.add(_Element())
    result = module._scan_uia(_Automation(root), "test-session")
    assert result.display_name is None
    assert result.evidence == "uia_scan_incomplete"


def test_node_budget_bounds_broad_tree(monkeypatch):
    monkeypatch.setattr(module, "_zoom_windows", lambda: [(100, 5)])
    root = _Element("Speaking: Alice")
    for _ in range(520):
        root.add(_Element())
    result = module._scan_uia(_Automation(root), "test-session")
    assert result.display_name is None
    assert result.evidence == "uia_scan_incomplete"

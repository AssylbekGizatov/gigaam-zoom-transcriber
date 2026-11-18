from dataclasses import replace

import pytest

from gigaam_zoom import zoom_window as window


class FakeWindows:
    def __init__(self):
        self.windows = {10: dict(title="Zoom Meeting", class_name="Meeting", path=r"C:\Zoom\Zoom.exe",
                                visible=True, minimized=False, cloaked=False, transparent=False,
                                bounds=(-1800, 100, -800, 700), shape=True)}
        self.order = [10]
        self.displays = [window.MonitorInfo("left", (-1920, 0, 0, 1080), (-1920, 0, 0, 1040), 96),
                         window.MonitorInfo("right", (0, 0, 2560, 1440), (0, 0, 2560, 1400), 144)]
        self.dpi_context = 7
        self.focus_calls = []

    def set_thread_dpi(self, context):
        previous, self.dpi_context = self.dpi_context, context
        return previous

    def handles(self):
        return list(self.order)

    def visible(self, hwnd):
        return self.windows[hwnd]["visible"]

    def minimized(self, hwnd):
        return self.windows[hwnd]["minimized"]

    def cloaked(self, hwnd):
        return self.windows[hwnd]["cloaked"]

    def transparent(self, hwnd):
        return self.windows[hwnd]["transparent"]

    def metadata(self, hwnd):
        return self.windows[hwnd]["title"], self.windows[hwnd]["class_name"]

    def process_path(self, hwnd):
        return self.windows[hwnd]["path"]

    def client_bounds(self, hwnd):
        return self.windows[hwnd]["bounds"]

    visible_bounds = client_bounds

    def pid(self, hwnd):
        return hwnd + 100

    def monitor_for_window(self, hwnd):
        return self.displays[0 if self.client_bounds(hwnd)[0] < 0 else 1]

    def monitors(self):
        return self.displays

    def previous_window(self, hwnd):
        index = self.order.index(hwnd)
        return self.order[index - 1] if index else 0

    def shape_intersects(self, hwnd, region):
        return self.windows[hwnd]["shape"]

    def bring_forward(self, hwnd):
        self.focus_calls.append(hwnd)
        self.windows[hwnd]["minimized"] = False
        return True

    def overlay(self, **kwargs):
        self.windows[11] = {**self.windows[10], "title": "Other app", "path": r"C:\Other.exe",
                            "bounds": (-1551, 221, -1549, 223), **kwargs}
        self.order.insert(0, 11)


@pytest.fixture
def native(monkeypatch):
    api = FakeWindows()
    monkeypatch.setattr(window, "_api", lambda: api)
    return api


@pytest.mark.parametrize("title,cls,expected", [
    ("Zoom Meeting", "Meeting", True),
    ("Конференция Zoom", "Meeting", True),
    ("Custom meeting name", "ZPContentViewWndClass", True),
    ("Zoom Meeting", "ZPToolBarParentWnd", False),
    ("Zoom Workplace", "ZPHOSTWND", False),
    ("Zoom", "Meeting", False),
    ("", "Popup", False),
])
def test_window_allowlist(title, cls, expected):
    assert window.is_meeting_window(title, cls) is expected


def test_selection_preserves_negative_physical_coordinates_and_monitor_dpi(native):
    selected = window.find_meeting_window()
    assert selected.client_bounds == (-1800, 100, -800, 700)
    assert selected.monitor_id == "left"
    assert selected.dpi == 96
    assert native.dpi_context == 7
    assert native.focus_calls == []


def test_selection_redetects_geometry_monitor_and_dpi(native):
    first = window.find_meeting_window()
    native.windows[10]["bounds"] = (200, 300, 1700, 1200)
    second = window.find_meeting_window()
    assert second.hwnd == first.hwnd
    assert second.client_bounds == (200, 300, 1700, 1200)
    assert second.monitor_id == "right"
    assert second.dpi == 144


def test_spoofed_title_from_other_process_is_not_zoom(native):
    native.windows[10]["path"] = r"C:\notzoom.exe"
    with pytest.raises(window.WindowProbeError, match="zoom_window_not_found"):
        window.find_meeting_window()


def test_multiple_plausible_meetings_are_ambiguous_even_if_one_minimized(native):
    native.overlay(title="Zoom Meeting", path=r"C:\Zoom.exe", minimized=True)
    with pytest.raises(window.WindowProbeError, match="zoom_window_ambiguous"):
        window.find_meeting_window()
    with pytest.raises(window.WindowProbeError, match="zoom_window_ambiguous"):
        window.get_meeting_window(10)


@pytest.mark.parametrize("state,reason", [("minimized", "ocr_window_minimized"),
                                         ("cloaked", "zoom_window_not_found"),
                                         ("visible", "zoom_window_not_found")])
def test_unusable_windows_fail_closed(native, state, reason):
    native.windows[10][state] = state != "visible"
    with pytest.raises(window.WindowProbeError, match=reason):
        window.find_meeting_window()
    assert native.dpi_context == 7


def test_transient_selection_rejects_replaced_handle(native):
    native.windows[12] = native.windows.pop(10)
    native.order = [12]
    with pytest.raises(window.WindowProbeError, match="ocr_window_changed"):
        window.get_meeting_window(10)


def test_nonforeground_zoom_with_clear_region_is_usable(native):
    native.overlay(bounds=(100, 100, 200, 200))
    window.ensure_region_uncovered(10, (-1600, 200, -1100, 240))
    assert native.focus_calls == []


def test_tiny_occluder_between_point_samples_is_detected(native):
    native.overlay()
    with pytest.raises(window.WindowProbeError, match="ocr_region_occluded"):
        window.ensure_region_uncovered(10, (-1600, 200, -1100, 240))


@pytest.mark.parametrize("override", [dict(visible=False), dict(minimized=True), dict(cloaked=True),
                                       dict(transparent=True), dict(shape=False)])
def test_nonpainting_or_nonintersecting_window_shape_does_not_occlude(native, override):
    native.overlay(**override)
    window.ensure_region_uncovered(10, (-1600, 200, -1100, 240))


def test_window_below_zoom_does_not_occlude(native):
    native.overlay()
    native.order = [10, 11]
    window.ensure_region_uncovered(10, (-1600, 200, -1100, 240))


def test_occlusion_walk_cycle_fails_closed(native):
    native.previous_window = lambda _: 10
    with pytest.raises(window.WindowProbeError, match="ocr_capture_unavailable"):
        window.ensure_region_uncovered(10, (-1600, 200, -1100, 240))


def test_capture_outside_client_area_is_rejected(native):
    with pytest.raises(window.WindowProbeError, match="ocr_window_changed"):
        window.ensure_region_uncovered(10, (-1900, 200, -1100, 240))


def test_capture_in_monitor_gap_is_rejected(native):
    native.displays[0] = replace(native.displays[0], bounds=(-1920, 0, -1300, 1080))
    with pytest.raises(window.WindowProbeError, match="ocr_region_offscreen"):
        window.ensure_region_uncovered(10, (-1600, 200, -1100, 240))


def test_monitor_union_coverage_handles_seam_and_gap():
    assert window.covered_by_monitors((-5, 1, 5, 5), [(-10, 0, 0, 10), (0, 0, 10, 10)])
    assert not window.covered_by_monitors((-5, 1, 5, 5), [(-10, 0, -1, 10), (0, 0, 10, 10)])
    assert not window.covered_by_monitors((0, 0, 0, 0), [(0, 0, 10, 10)])


def test_bring_forward_is_only_explicit_and_restores_minimized_meeting(native):
    native.windows[10]["minimized"] = True
    selected = window.bring_meeting_to_front()
    assert selected.hwnd == 10
    assert native.focus_calls == [10]


def test_failed_dpi_context_prevents_capture(native):
    native.set_thread_dpi = lambda _: 0
    with pytest.raises(window.WindowProbeError, match="ocr_dpi_unavailable"):
        window.find_meeting_window()


def test_seven_visible_zoom_windows_discard_six_zero_area_surfaces(native):
    native.windows[10].update(title="Еженедельная встреча команды", class_name="ZPContentViewWndClassV7")
    for hwnd in range(20, 26):
        native.windows[hwnd] = {**native.windows[10], "title": "Zoom Meeting", "bounds": (0, 0, 0, 0)}
    native.order = [20, 21, 22, 23, 24, 25, 10]
    metadata = native.metadata
    read_metadata = []
    def record_metadata(hwnd):
        read_metadata.append(hwnd)
        return metadata(hwnd)
    native.metadata = record_metadata
    selected = window.find_meeting_window()
    assert selected.hwnd == 10
    assert read_metadata == [10]  # Filter dimensions before interpreting any title.


@pytest.mark.parametrize("bounds", [(0, 0, 0, 0), (0, 0, 99, 800), (0, 0, 1000, 79)])
def test_tiny_main_window_is_not_a_candidate(native, bounds):
    native.windows[10]["bounds"] = bounds
    with pytest.raises(window.WindowProbeError, match="zoom_window_not_found"):
        window.find_meeting_window()


def test_larger_workplace_home_never_outranks_meeting(native):
    native.overlay(title="Zoom Workplace", class_name="ZPContentViewWndClass", path=r"C:\Zoom.exe",
                   bounds=(0, 0, 2400, 1300))
    assert window.find_meeting_window().hwnd == 10


def test_largest_valid_auxiliary_surface_wins_when_no_main_root(native):
    native.windows[10].update(title="", class_name="ZPFloatVideoWndClass")
    native.overlay(title="", class_name="ZPVideoViewWndClass", path=r"C:\Zoom.exe",
                   bounds=(-1800, 100, -600, 800))
    assert window.find_meeting_window().hwnd == 11


def test_main_root_outranks_larger_auxiliary_surface(native):
    native.overlay(title="", class_name="ZPFloatVideoWndClass", path=r"C:\Zoom.exe",
                   bounds=(0, 0, 2400, 1300))
    assert window.find_meeting_window().hwnd == 10


def test_equal_largest_auxiliary_surfaces_are_ambiguous(native):
    native.windows[10].update(title="", class_name="ZPFloatVideoWndClass")
    native.overlay(title="", class_name="ZPVideoViewWndClass", path=r"C:\Zoom.exe",
                   bounds=(0, 0, 1000, 600))
    with pytest.raises(window.WindowProbeError, match="zoom_window_ambiguous"):
        window.find_meeting_window()


def test_two_main_meetings_remain_ambiguous_even_with_different_areas(native):
    native.overlay(title="Zoom Meeting", class_name="ZPContentViewWndClass", path=r"C:\Zoom.exe",
                   bounds=(0, 0, 2400, 1300))
    with pytest.raises(window.WindowProbeError, match="zoom_window_ambiguous"):
        window.find_meeting_window()


def test_minimized_main_with_zero_client_size_reports_minimized(native):
    native.windows[10].update(minimized=True, bounds=(0, 0, 0, 0))
    with pytest.raises(window.WindowProbeError, match="ocr_window_minimized"):
        window.find_meeting_window()


def test_recreated_meeting_is_automatically_redetected_without_cached_pid_or_hwnd(native):
    before = window.find_meeting_window()
    native.windows[73] = native.windows.pop(10)
    native.order = [73]
    after = window.find_meeting_window()
    assert after.hwnd == 73
    assert after.pid != before.pid


@pytest.mark.parametrize("title,class_name", [
    ("Junta del equipo", "ZPContentViewWndClassV7"),
    ("会議", "ZPMeetingWndClass"),
    ("Réunion Zoom", "NewMeetingClientClass"),
])
def test_localized_custom_titles_and_meeting_class_variations(native, title, class_name):
    native.windows[10].update(title=title, class_name=class_name)
    assert window.find_meeting_window().hwnd == 10

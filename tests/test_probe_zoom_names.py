import json

import pytest

from scripts import probe_zoom_names as probe


def test_probe_defaults_to_ocr_and_auto_installed_language():
    args = probe.parse_args(["--region", ".05,.8,.4,.05"])
    assert args.language == "auto"
    assert args.duration == 15


def test_probe_parses_normalized_region_and_calibrated_size():
    args = probe.parse_args(["--region", ".05,.8,.4,.05",
                             "--client-size", "1920,1080", "--language", "en-US"])
    assert args.region == (.05, .8, .4, .05)
    assert args.client_size == (1920, 1080)
    assert args.language == "en-US"


@pytest.mark.parametrize("arguments", [
    [],
    ["--region", "0,0,0.5"],
    ["--region", "nan,0,.5,.5"],
    ["--region", "0,0,inf,.5"],
    ["--region", "0,0,0,.5"],
    ["--region", ".9,0,.5,.5"],
    ["--region=-.1,0,.5,.5"],
    ["--region", "a,b,c,d"],
    ["--region", "0,0,.5,.5", "--client-size", "0,1080"],
    ["--region", "0,0,.5,.5", "--client-size", "12.5,1080"],
    ["--duration", "nan"],
    ["--duration", "0"],
    ["--duration", "601"],
    ["--language", ""],
])
def test_probe_rejects_unsafe_or_inapplicable_arguments(arguments):
    with pytest.raises(SystemExit) as result:
        probe.parse_args(arguments)
    assert result.value.code == 2


def test_language_listing_does_not_require_region():
    assert probe.parse_args(["--list-languages"]).list_languages


def test_non_windows_probe_returns_actionable_diagnostic(monkeypatch, capsys):
    monkeypatch.setattr(probe.sys, "platform", "linux")
    assert probe.main(["--list-languages"]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["evidence"] == "unsupported_platform"
    assert "Windows" in output.err


def test_user_full_frame_diagnostic_command_and_opt_in_png():
    args = probe.parse_args(["--region", "0,0,1,1", "--language", "en-US",
                             "--dump-ocr-text", "--duration", "15"])
    assert args.region == (0, 0, 1, 1)
    assert args.dump_ocr_text
    assert args.save_debug_frame is None
    args = probe.parse_args(["--region", "0,0,1,1", "--save-debug-frame", "crop.png"])
    assert str(args.save_debug_frame) == "crop.png"

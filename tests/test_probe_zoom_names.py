import json

import pytest

from scripts import probe_zoom_names as probe


def test_probe_uia_defaults_do_not_require_ocr_dependencies():
    args = probe.parse_args([])
    assert args.mode == "uia"
    assert args.region is None
    assert args.duration == 15


def test_probe_parses_normalized_region_and_calibrated_size():
    args = probe.parse_args(["--mode", "ocr", "--region", ".05,.8,.4,.05",
                             "--client-size", "1920,1080", "--language", "en-US"])
    assert args.region == (.05, .8, .4, .05)
    assert args.client_size == (1920, 1080)
    assert args.language == "en-US"


@pytest.mark.parametrize("arguments", [
    ["--mode", "ocr"],
    ["--mode", "ocr", "--region", "0,0,0.5"],
    ["--mode", "ocr", "--region", "nan,0,.5,.5"],
    ["--mode", "ocr", "--region", "0,0,inf,.5"],
    ["--mode", "ocr", "--region", "0,0,0,.5"],
    ["--mode", "ocr", "--region", ".9,0,.5,.5"],
    ["--mode", "ocr", "--region=-.1,0,.5,.5"],
    ["--mode", "ocr", "--region", "a,b,c,d"],
    ["--mode", "ocr", "--region", "0,0,.5,.5", "--client-size", "0,1080"],
    ["--mode", "ocr", "--region", "0,0,.5,.5", "--client-size", "12.5,1080"],
    ["--region", "0,0,.5,.5"],
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
    assert probe.parse_args(["--mode", "ocr", "--list-languages"]).list_languages


def test_non_windows_probe_returns_actionable_diagnostic(monkeypatch, capsys):
    monkeypatch.setattr(probe.sys, "platform", "linux")
    assert probe.main([]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["evidence"] == "unsupported_platform"
    assert "Windows" in output.err

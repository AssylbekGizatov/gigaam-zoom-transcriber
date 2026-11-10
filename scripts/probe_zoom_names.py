"""Read Zoom speaker observations locally without recording audio or images.

Example (Windows, from the repository root):
  py -3.13 scripts/probe_zoom_names.py --mode uia --duration 15
  py -3.13 scripts/probe_zoom_names.py --list-languages
  py -3.13 scripts/probe_zoom_names.py --mode ocr --region .02,.85,.25,.05

The OCR region is x,y,width,height as fractions of the Zoom meeting client
area. Use an unpinned Speaker View and select only its displayed speaker name.
Keep Zoom foreground during observation. Output includes participant names;
review/redact it before sharing. No network requests or screenshot files.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _region(value: str) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(item.strip()) for item in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("region must be x,y,width,height fractions") from error
    if len(parts) != 4 or not all(math.isfinite(item) for item in parts):
        raise argparse.ArgumentTypeError("region must contain four finite numbers")
    x, y, width, height = parts
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        raise argparse.ArgumentTypeError("region must fit within the client area (0..1)")
    return parts


def _client_size(value: str) -> tuple[int, int]:
    try:
        parts = tuple(int(item.strip()) for item in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("client size must be width,height in pixels") from error
    if len(parts) != 2 or any(item <= 0 for item in parts):
        raise argparse.ArgumentTypeError("client size must contain two positive integers")
    return parts


def _duration(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("duration must be seconds") from error
    if not math.isfinite(seconds) or not 0 < seconds <= 600:
        raise argparse.ArgumentTypeError("duration must be greater than 0 and at most 600 seconds")
    return seconds


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("uia", "ocr"), default="uia")
    parser.add_argument("--region", type=_region,
                        help="x,y,width,height fractions of the Zoom client area; required for OCR")
    parser.add_argument("--language", default="ru-RU", help="installed Windows OCR language tag")
    parser.add_argument("--client-size", type=_client_size,
                        help="optional calibrated client width,height in physical pixels")
    parser.add_argument("--duration", type=_duration, default=15.0)
    parser.add_argument("--list-languages", action="store_true",
                        help="list installed Windows OCR languages and exit")
    args = parser.parse_args(argv)
    if args.mode == "ocr" and args.region is None and not args.list_languages:
        parser.error("--mode ocr requires --region x,y,width,height")
    if args.mode == "uia" and (args.region is not None or args.client_size is not None):
        parser.error("--region and --client-size apply only to --mode ocr")
    if not args.language.strip():
        parser.error("--language must not be empty")
    return args


def _print_json(value):
    # ASCII escapes also work in corporate terminals using legacy code pages.
    print(json.dumps(value, ensure_ascii=True), flush=True)


def _installed_languages():
    from winrt.windows.media.ocr import OcrEngine
    return [{"tag": language.language_tag, "name": language.display_name}
            for language in OcrEngine.available_recognizer_languages]


def main(argv=None) -> int:
    args = parse_args(argv)
    if sys.platform != "win32":
        _print_json({"evidence": "unsupported_platform", "name": None})
        print("Zoom name diagnostics require Windows with the desktop Zoom client.", file=sys.stderr)
        return 2
    if args.list_languages:
        try:
            _print_json({"ocr_languages": _installed_languages()})
            return 0
        except Exception as error:
            _print_json({"evidence": "ocr_unavailable", "error_type": type(error).__name__})
            print("Install requirements-zoom-ocr.txt and an approved Windows OCR language pack.",
                  file=sys.stderr)
            return 2

    from gigaam_zoom.zoom_accessibility import ZoomSpeakerObserver
    config = None
    if args.mode == "ocr":
        from gigaam_zoom.zoom_visual import VisualSpeakerConfig
        try:
            config = VisualSpeakerConfig(region=args.region, language=args.language,
                                         client_size=args.client_size)
            config.validate()
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
    print("Output contains Zoom participant names. Review/redact it before sharing. "
          "No audio or screenshot files are written.", file=sys.stderr)
    if config is not None:
        print("Use unpinned Speaker View with the speaker name visible; bring Zoom to the "
              "foreground now. The region must contain only that name.", file=sys.stderr)
    observer = ZoomSpeakerObserver(visual_config=config)
    started = time.monotonic()
    previous = None
    try:
        observer.start()
        while time.monotonic() - started < args.duration:
            observation = observer.latest()
            if observation != previous:
                _print_json({"elapsed_ms": round((time.monotonic() - started) * 1000),
                             "observed_at_ms": observation.observed_at_ms,
                             "name": observation.display_name,
                             "evidence": observation.evidence})
                previous = observation
            time.sleep(.1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

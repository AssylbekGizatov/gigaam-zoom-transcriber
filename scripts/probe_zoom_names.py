"""Inspect the same local OCR backend used by preview and transcription.

Example (Windows, from the repository root):
  py -3.13 scripts/probe_zoom_names.py --list-languages
  py -3.13 scripts/probe_zoom_names.py --region .02,.85,.25,.05
  py -3.13 scripts/probe_zoom_names.py --region 0,0,1,1 --language en-US --dump-ocr-text

The OCR region is x,y,width,height as fractions of the Zoom meeting client
area. Use an unpinned Speaker View and select only its displayed speaker name.
Keep the crop visible and uncovered. Full-window OCR is for diagnostics only;
normal transcription requires the calibrated nameplate. Raw OCR output and
PNG saving are opt-in. No network requests.
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
    parser.add_argument("--region", type=_region,
                        help="x,y,width,height fractions of the Zoom client area; required for OCR")
    parser.add_argument("--language", default="auto", help="installed OCR tag; auto prefers ru, then en-US")
    parser.add_argument("--client-size", type=_client_size,
                        help="optional calibrated client width,height in physical pixels")
    parser.add_argument("--duration", type=_duration, default=15.0)
    parser.add_argument("--dump-ocr-text", action="store_true",
                        help="print every raw OCR line before filtering plus per-frame timings and geometry")
    parser.add_argument("--save-debug-frame", type=Path, metavar="PATH",
                        help="explicitly save the first captured crop as PNG at PATH")
    parser.add_argument("--list-languages", action="store_true",
                        help="list installed Windows OCR languages and exit")
    args = parser.parse_args(argv)
    if args.region is None and not args.list_languages:
        parser.error("OCR requires --region x,y,width,height")
    if not args.language.strip():
        parser.error("--language must not be empty")
    return args


def _print_json(value):
    # ASCII escapes also work in corporate terminals using legacy code pages.
    print(json.dumps(value, ensure_ascii=True), flush=True)


def _installed_languages():
    from gigaam_zoom.zoom_visual import installed_ocr_languages
    return installed_ocr_languages()


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
            print("Install requirements-desktop.txt and an approved Windows OCR language pack.",
                  file=sys.stderr)
            return 2

    from gigaam_zoom.speaker_observer import ZoomSpeakerObserver
    from gigaam_zoom.zoom_visual import VisualSpeakerConfig
    try:
        config = VisualSpeakerConfig(region=args.region, language=args.language,
                                     client_size=args.client_size,
                                     dump_ocr_text=args.dump_ocr_text,
                                     include_image=args.save_debug_frame is not None)
        config.validate()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print("Output contains Zoom participant names. Review/redact it before sharing. "
          "Raw lines print only with --dump-ocr-text; PNG writes only with --save-debug-frame.", file=sys.stderr)
    print("Use unpinned Speaker View with an uncovered nameplate. Full-window --region "
          "0,0,1,1 is a debug probe, not a calibrated speaker region.", file=sys.stderr)
    observer = ZoomSpeakerObserver(visual_config=config, log_samples=False)
    started = time.monotonic()
    previous = None
    saved = False
    try:
        observer.start()
        while time.monotonic() - started < args.duration:
            frame = observer.latest_frame()
            if frame is not None and frame is not previous:
                observation = frame.observation
                output = (frame.diagnostic_dict() if args.dump_ocr_text else {
                    "observed_at_ms": observation.observed_at_ms,
                    "name": observation.display_name, "evidence": observation.evidence,
                    "timings": frame.timings, "max_age_ms": frame.max_age_ms,
                })
                output["elapsed_ms"] = round((time.monotonic() - started) * 1000)
                _print_json(output)
                if args.save_debug_frame is not None and not saved and frame.image_png:
                    args.save_debug_frame.write_bytes(frame.image_png)
                    saved = True
                    print("Debug crop saved to the requested path.", file=sys.stderr)
                previous = frame
            elif frame is None:
                observation = observer.latest()
                if observation != previous:
                    _print_json({"evidence": observation.evidence, "name": observation.display_name})
                    previous = observation
            time.sleep(.1)
    except KeyboardInterrupt:
        pass
    except OSError as error:
        print(f"Could not save the requested debug frame ({type(error).__name__}).", file=sys.stderr)
        return 2
    finally:
        observer.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Exercise real WASAPI with an explicitly chosen public WAV, never save audio.

Run with --fixture path/to/public.wav. Playback uses the current Windows default
output; selected loopback must correspond to that output. Microphone is disabled.
Only numerical diagnostics are written to stdout. This is not a Zoom test.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from gigaam_zoom.desktop_audio import DesktopCapture, list_devices


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--device", type=int)
    args = parser.parse_args()
    if args.fixture is None:
        print(json.dumps([asdict(d) for d in list_devices()], ensure_ascii=True, indent=2))
        return
    import winsound
    with wave.open(str(args.fixture), "rb") as fixture:
        duration = fixture.getnframes() / fixture.getframerate()
        if not 0 < duration <= 60:
            parser.error("Fixture must be at most 60 seconds")
    result = {"fixture_seconds": duration, "microphone_enabled": False,
              "audio_files_written": 0, "packets": 0, "peak_rms": 0.0,
              "first_timestamp_ms": None, "last_timestamp_ms": None}
    events = []
    def accept(source, pcm, stamp, rate):
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        result["packets"] += 1
        result["peak_rms"] = max(result["peak_rms"], float(np.sqrt(np.mean(samples * samples))))
        if result["first_timestamp_ms"] is None:
            result["first_timestamp_ms"] = stamp
        result["last_timestamp_ms"] = stamp + len(samples) * 1000 / rate
    capture = DesktopCapture(args.device, None, accept, lambda kind, payload: events.append({"kind": kind, **payload}))
    begin = time.monotonic()
    try:
        capture.start()
        time.sleep(.5)
        winsound.PlaySound(str(args.fixture.resolve()), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        time.sleep(duration + .5)
    finally:
        winsound.PlaySound(None, 0)
        capture.stop()
    result.update(elapsed_seconds=time.monotonic() - begin, metrics=capture.metrics, events=events,
                  verified_scope="public fixture playback through real Windows output loopback; not a Zoom meeting")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    if result["peak_rms"] < .001 or result["packets"] < 10 or capture.metrics["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

"""Local, reproducible public-audio measurements; never opens Zoom or a mic.

The manifest must explicitly identify permitted local PCM16 mono 16 kHz clips,
their hashes, references and provenance. No audio or credentials are downloaded
by this script. It measures both the engine and the actual participant pipeline.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import socket
import sys
import tempfile
import threading
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def normalize(text):
    return re.findall(r"[^\W_]+", text.casefold().replace("ё", "е"), re.UNICODE)


def distance(reference, hypothesis):
    row = list(range(len(hypothesis) + 1))
    for index, word in enumerate(reference, 1):
        previous, row = row, [index]
        for offset, other in enumerate(hypothesis, 1):
            row.append(min(row[-1] + 1, previous[offset] + 1,
                           previous[offset - 1] + (word != other)))
    return row[-1]


def score(reference, hypothesis):
    ref, hyp = normalize(reference), normalize(hypothesis)
    edits = distance(ref, hyp)
    return {"reference_words": len(ref), "word_edits": edits,
            "wer": edits / len(ref) if ref else None,
            "normalization": "Unicode casefold; ё to е; punctuation removed; digits preserved"}


def rss_bytes():
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except ImportError:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
            value = Counters()
            value.cb = ctypes.sizeof(value)
            process = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(value), value.cb):
                return value.WorkingSetSize
        return None


class Measurement:
    def __enter__(self):
        self.started = time.perf_counter()
        cpu = os.times()
        self.cpu_start = cpu.user + cpu.system
        self.peak = rss_bytes()
        self.done = threading.Event()
        def sample():
            while not self.done.wait(.05):
                value = rss_bytes()
                if value is not None:
                    self.peak = max(self.peak or 0, value)
        self.thread = threading.Thread(target=sample, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.wall = time.perf_counter() - self.started
        cpu = os.times()
        self.cpu_seconds = cpu.user + cpu.system - self.cpu_start
        self.done.set()
        self.thread.join(2)
        value = rss_bytes()
        if value is not None:
            self.peak = max(self.peak or 0, value)

    def result(self, duration=None):
        return {"wall_seconds": self.wall, "process_cpu_seconds": self.cpu_seconds,
                "cpu_percent_one_core": self.cpu_seconds / max(self.wall, 1e-9) * 100,
                "cpu_percent_machine": self.cpu_seconds / max(self.wall, 1e-9) * 100 / (os.cpu_count() or 1),
                "sampled_peak_rss_mib": self.peak / 1048576 if self.peak is not None else None,
                "rss_sampling_interval_seconds": .05,
                "real_time_factor": self.wall / duration if duration else None}


@contextmanager
def no_python_network():
    """Catch accidental Python socket egress during local inference.

    This is an auditable smoke check, not an operating-system firewall proof.
    """
    attempted = []
    original = socket.socket.connect, socket.socket.connect_ex, socket.create_connection
    def blocked(*args, **kwargs):
        attempted.append("socket connection blocked")
        raise RuntimeError("Network forbidden during local ASR evaluation")
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = blocked
    try:
        yield attempted
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = original


def read_clip(item, manifest_dir):
    import numpy as np
    path = Path(item["path"])
    if not path.is_absolute():
        path = manifest_dir / path
    if path.stat().st_size > 20_000_000:
        raise ValueError("Each public test clip is limited to 20 MB")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != item["sha256"]:
        raise ValueError("Fixture SHA256 mismatch")
    for key in ("language", "reference", "source_url", "license", "attribution"):
        if not item.get(key):
            raise ValueError("Manifest missing provenance field: " + key)
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise ValueError("Evaluation accepts only PCM16 mono 16 kHz WAV")
        if audio.getnframes() > 20 * 16000:
            raise ValueError("Each public test clip is limited to 20 seconds")
        return np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").copy()


def pipeline_run(engine, samples, reference, pace):
    from gigaam_zoom.models import AudioPacket
    from gigaam_zoom.pipeline import Pipeline
    from gigaam_zoom.store import Store
    events, result_delays = [], []
    first_result = None
    started = time.perf_counter()
    def event(kind, value):
        nonlocal first_result
        now = time.perf_counter() - started
        if kind == "segment":
            first_result = now if first_result is None else first_result
            if pace == "realtime":
                result_delays.append(now - value.end)
        elif kind not in ("progress", "capture_timestamp"):
            events.append(kind)
    with tempfile.TemporaryDirectory(prefix="gigaam-eval-") as directory:
        store = Store(Path(directory) / "public-evaluation.sqlite3")
        pipeline = Pipeline(store, engine, event, origin_ms=0)
        pipeline.start()
        started = time.perf_counter()
        with Measurement() as measurement:
            for frame in range(0, len(samples), 1600):
                if pace == "realtime":
                    target = min(frame + 1600, len(samples)) / 16000
                    delay = target - (time.perf_counter() - started)
                    if delay > 0:
                        time.sleep(delay)
                if not pipeline.feed(AudioPacket("public-source-1", "Public fixture speaker", frame // 16,
                                                 samples[frame:frame + 1600].tobytes(), epoch="test-1")):
                    raise RuntimeError("Pipeline rejected a valid public audio packet")
            pipeline.stop()
        records = list(store.iter_segments())
        hypothesis = " ".join(row.text for row in records)
        gaps = list(store.iter_gaps())
        metrics = pipeline.metrics
        store.close()
    return {"pace": pace, "hypothesis": hypothesis, **score(reference, hypothesis),
            "measurement": measurement.result(len(samples) / 16000),
            "pipeline_metrics": metrics, "segment_count": len(records),
            "speaker_ids": sorted({row.speaker_id for row in records}),
            "timestamps_valid": all(0 <= row.start < row.end <= len(samples) / 16000 + .05 for row in records),
            "gap_count": len(gaps), "events": events,
            "first_result_wall_seconds": first_result,
            "result_after_utterance_end_seconds": result_delays,
            "latency_scope": ("Local paced public PCM input to final segment callback; excludes Zoom/network"
                              if pace == "realtime" else "Burst replay throughput; not live latency")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--pace", choices=("burst", "realtime"), default="burst")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 20:
        raise ValueError("repeat must be 1..20")
    if args.report.exists():
        raise FileExistsError("Choose a new report file")
    import numpy as np
    from gigaam_zoom.engine import CODE_REVISION, MODEL_FILES, MODEL_REVISION, Transcriber
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    clips = [(item, read_clip(item, args.manifest.parent)) for item in manifest["clips"]]
    versions = {}
    for package in ("numpy", "torch", "torchaudio", "gigaam", "python-docx", "reportlab"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "not recorded by package metadata"
    report = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "Public read-speech smoke tests and synthetic concatenation, not a benchmark",
              "live_zoom_meeting_tested": False, "natural_code_switching_tested": False,
              "simultaneous_speech_tested": False,
              "machine": {"os": platform.system(), "release": platform.release(),
                          "architecture": platform.machine(), "processor": platform.processor(),
                          "logical_cpus": os.cpu_count(), "python": platform.python_version()},
              "versions": versions, "model_revision": MODEL_REVISION, "code_revision": CODE_REVISION,
              "model_sha256": MODEL_FILES, "samples": []}
    engine = Transcriber(args.model_dir)
    with no_python_network() as network:
        with Measurement() as load_measurement:
            engine.load()
        report["cold_model_load"] = load_measurement.result()
        for item, pcm in clips:
            results = []
            for _ in range(args.repeat):
                with Measurement() as measurement:
                    words = engine.transcribe_words(pcm.astype(np.float32) / 32768, 16000)
                hypothesis = " ".join(word.text for word in words)
                results.append({"hypothesis": hypothesis, **score(item["reference"], hypothesis),
                                "measurement": measurement.result(len(pcm) / 16000),
                                "word_timestamps_valid": all(0 <= word.start < word.end <= len(pcm) / 16000 + .05 for word in words)})
            public_item = {key: value for key, value in item.items() if key != "path"}
            report["samples"].append({"fixture": public_item, "duration_seconds": len(pcm) / 16000,
                                      "engine_runs": results,
                                      "pipeline": pipeline_run(engine, pcm, item["reference"], args.pace)})
        # A splice of independently recorded languages checks routing and seams;
        # it cannot establish quality on natural human code switching.
        mixed = np.concatenate([part for _, pcm in clips for part in (pcm, np.zeros(5600, dtype="<i2"))])
        if len(mixed) / 16000 > 40:
            raise ValueError("Combined fixtures exceed bounded 40-second test")
        reference = " ".join(item["reference"] for item, _ in clips)
        report["synthetic_mixed_pipeline"] = pipeline_run(engine, mixed, reference, args.pace)
        report["blocked_python_network_attempts"] = len(network)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    print("Public audio evaluation complete. Live Zoom meeting: NOT TESTED.")


if __name__ == "__main__":
    main()

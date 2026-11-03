"""Reproduce local CPU calibration on licensed public fixtures; writes no audio."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
import threading
import time
import wave

import numpy as np
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gigaam_zoom.diarization import (
    DiarizationTimeline, MODEL_BYTES, MODEL_CARD, MODEL_LICENSE, MODEL_SHA256,
    OnlineDiarizer, SAMPLE_RATE, SherpaSpeakerEmbedder,
)


class Meter:
    def __enter__(self):
        self.process = psutil.Process()
        self.baseline = self.peak = self.process.memory_info().rss
        self.wall = time.perf_counter()
        self.cpu = sum(self.process.cpu_times()[:2])
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()
        return self

    def sample(self):
        while not self.stop.wait(.02):
            self.peak = max(self.peak, self.process.memory_info().rss)

    def __exit__(self, *_):
        wall = time.perf_counter() - self.wall
        cpu = sum(self.process.cpu_times()[:2]) - self.cpu
        self.stop.set()
        self.thread.join()
        self.peak = max(self.peak, self.process.memory_info().rss)
        self.result = {"wall_seconds": wall, "cpu_seconds": cpu,
                       "cpu_percent_one_core": cpu / wall * 100,
                       "cpu_percent_machine": cpu / wall / (psutil.cpu_count() or 1) * 100,
                       "baseline_rss_mib": self.baseline / 2**20,
                       "sampled_peak_rss_mib": self.peak / 2**20,
                       "sampled_rss_delta_mib": (self.peak - self.baseline) / 2**20,
                       "rss_sample_interval_seconds": .02}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--model", type=Path, default=Path("models/diarization/campplus.onnx"))
    parser.add_argument("--output", type=Path,
                        help="Defaults to docs/evidence/desktop-diarization-MODEL.json")
    args = parser.parse_args()
    manifest = json.loads((args.fixtures / "manifest.json").read_text(encoding="utf-8"))
    clips = {}
    sources = []
    for clip in manifest["clips"]:
        path = args.fixtures / clip["path"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != clip["sha256"]:
            raise ValueError("Public fixture hash mismatch")
        with wave.open(str(path)) as stream:
            if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
                raise ValueError("Expected mono PCM16 16 kHz public fixtures")
            clips[clip["language"]] = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32) / 32768
        sources.append({key: clip[key] for key in ("language", "source_url", "license", "attribution", "sha256")})
    intervals = []
    inputs = []
    position = 0
    for index, language in enumerate(("kk", "ru", "en", "kk")):
        clip = clips[language]
        intervals.append({"language": language, "repeat": index == 3,
                          "start": position / SAMPLE_RATE,
                          "end": (position + len(clip)) / SAMPLE_RATE})
        inputs.extend((clip, np.zeros(2 * SAMPLE_RATE, dtype=np.float32)))
        position += len(clip) + 2 * SAMPLE_RATE
    audio = np.concatenate(inputs)
    with Meter() as load_meter:
        embedder = SherpaSpeakerEmbedder(args.model)
        embedder.load()
    diarizer = OnlineDiarizer(embedder)
    timeline = DiarizationTimeline(diarizer)
    per_window = []
    with Meter() as inference_meter:
        for start in range(0, len(audio), 6 * SAMPLE_RATE):
            before = time.perf_counter()
            timeline.analyze_window("local:remote", audio[start:start + 8 * SAMPLE_RATE],
                                    SAMPLE_RATE, start / SAMPLE_RATE)
            per_window.append(time.perf_counter() - before)
        timeline.flush()
    results = []
    for interval in intervals:
        counts = Counter()
        for span in timeline._spans:
            duration = max(0, min(interval["end"], span.end) - max(interval["start"], span.start))
            if duration:
                key = span.local_speaker_id if span.confidence >= .60 else None
                counts[key or "unknown"] += duration
        known = {key: value for key, value in counts.items() if key != "unknown"}
        dominant = max(known, key=known.get) if known else None
        results.append({**interval, "dominant_local_cluster": dominant,
                        "seconds_by_cluster_including_silence": dict(counts)})
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Real CPU ONNX embeddings; synthetic chronological concatenation of public speech",
        "live_zoom_meeting_tested": False, "independent_heldout_test": False,
        "calibration_data": True, "audio_files_written": 0,
        "model": {"name": embedder.model_spec.model_id,
                  "bytes": embedder.model_spec.size_bytes, "sha256": embedder.model_spec.sha256,
                  "license": embedder.model_spec.license, "model_card": embedder.model_spec.model_card,
                  "attribution": embedder.model_spec.attribution},
        "versions": {name: importlib.metadata.version(name) for name in ("sherpa-onnx", "sherpa-onnx-core", "numpy", "psutil")},
        "machine": {"os": platform.system(), "release": platform.release(),
                    "logical_cpus": psutil.cpu_count(), "physical_memory_mib": psutil.virtual_memory().total / 2**20},
        "settings": {"window_seconds": diarizer.window_samples / SAMPLE_RATE, "hop_seconds": 1,
                     "match_threshold": diarizer.match_threshold, "new_threshold": diarizer.new_threshold,
                     "margin": diarizer.margin, "half_similarity": diarizer.half_similarity,
                     "max_speakers": diarizer.max_speakers, "cpu_threads": 1},
        "load": load_meter.result,
        "inference": {**inference_meter.result, "audio_seconds": len(audio) / SAMPLE_RATE,
                      "real_time_factor": inference_meter.result["wall_seconds"] / (len(audio) / SAMPLE_RATE),
                      "asr_window_processing_p50_seconds": float(np.median(per_window)),
                      "asr_window_processing_max_seconds": max(per_window)},
        "cluster_count": diarizer.speaker_count, "clip_results": results,
        "three_distinct_dominant_clusters": len({r["dominant_local_cluster"] for r in results[:3] if r["dominant_local_cluster"]}) == 3,
        "identical_clip_repeat_reuses_cluster": results[0]["dominant_local_cluster"] is not None and results[0]["dominant_local_cluster"] == results[3]["dominant_local_cluster"],
        "spans": [asdict(s) for s in timeline._spans], "public_sources": sources,
        "limits": [
            "Model selection used these three public clips; this is development/calibration data, not independent accuracy validation",
            "WeSpeaker two-second windows merged KK/RU or fragmented RU; three-second calibration passed but live WASAPI still merged KK/RU",
            "Identical replay tests cluster persistence, not general same-person recognition on unseen speech",
            "Silence is included in clip duration; unknown duration is not a diarization error rate",
            "No Zoom ID or real participant name is inferred; labels are local voice clusters",
            "Simultaneous speakers, natural code switching, noisy meetings and headset/device changes are not validated",
            "No trained overlap detector; a mixed voice can be assigned incorrectly",
            "One background-loaded PC run; sampled RSS is not a guaranteed maximum",
        ],
    }
    output = args.output or Path(f"docs/evidence/desktop-diarization-{embedder.model_spec.model_id}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("cluster_count", "three_distinct_dominant_clusters", "identical_clip_repeat_reuses_cluster", "load", "inference")}, indent=2))


if __name__ == "__main__":
    main()

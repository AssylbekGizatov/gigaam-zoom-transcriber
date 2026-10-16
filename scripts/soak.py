"""Accelerated bounded-memory pipeline soak with generated PCM and fake ASR.

Run from the repository: python scripts/soak.py --hours 8
This does not open Zoom, download a model, or benchmark real recognition.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gc
import json
import math
from pathlib import Path
import platform
import sys
import tempfile
import time

import numpy as np
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gigaam_zoom.models import AudioPacket, Word
from gigaam_zoom.pipeline import Pipeline
from gigaam_zoom.store import Store


class GeneratedRecognizer:
    """Return deterministic placeholder words; never call a speech model."""

    def transcribe_words(self, samples, rate):
        duration = len(samples) / rate
        return [Word(start, start + .2, text)
                for start, text in ((.1, "generated"), (1.1, "fixture"),
                                    (3.1, "memory"), (5.1, "test"))
                if start + .2 <= duration]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=8.0)
    parser.add_argument("--sources", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/evidence/soak.json")
    parser.add_argument("--scratch", type=Path, default=ROOT / "work")
    args = parser.parse_args()
    if not math.isfinite(args.hours) or not 0 < args.hours <= 48:
        parser.error("--hours must be >0 and <=48")
    args.scratch.mkdir(parents=True, exist_ok=True)
    process = psutil.Process()
    events = Counter()
    samples = []
    origin = 1_700_000_000_000
    total_frames = round(args.hours * 3600 * 16000)
    chunk_frames = 8 * 16000
    # One immutable 8s signal is reused, rather than retaining the audio timeline.
    base_pcm = np.round(np.sin(np.arange(chunk_frames) * (2 * np.pi * 220 / 16000)) * 1200).astype("<i2").tobytes()
    def on_event(kind, _payload):
        events[kind] += 1

    def rss_mb():
        return round(process.memory_info().rss / 1024**2, 3)

    def disk_bytes(db_path):
        return sum(path.stat().st_size for path in db_path.parent.glob(db_path.name + "*") if path.is_file())

    start = time.perf_counter()
    cpu_start = process.cpu_times()
    initial_rss = rss_mb()
    peak_rss = initial_rss
    max_pending = 0.0
    refused_packets = 0
    max_recent = 0
    pacing_wait = 0.0
    packets = 0
    warmup_rss = None

    with tempfile.TemporaryDirectory(prefix="gigaam-soak-", dir=args.scratch) as temp:
        db_path = Path(temp) / "generated-session.sqlite"
        store = Store(db_path)
        pipeline = Pipeline(store, GeneratedRecognizer(), on_event, origin,
                            max_pending_seconds=48, idle_timeout_seconds=30)
        initial_disk = disk_bytes(db_path)
        pipeline.start()
        try:
            position = 0
            while position < total_frames:
                # Pace by accepted queued PCM, not by the simulated meeting
                # clock. This measures retained state without forcing overload.
                wait_start = time.perf_counter()
                deadline = wait_start + 30
                while pipeline.pending_seconds > 16:
                    if pipeline.failure:
                        raise RuntimeError(pipeline.failure)
                    if time.perf_counter() > deadline:
                        raise TimeoutError("Pipeline did not drain within 30 real seconds")
                    peak_rss = max(peak_rss, rss_mb())
                    time.sleep(.001)
                pacing_wait += time.perf_counter() - wait_start
                frames = min(chunk_frames, total_frames - position)
                uid = str(100 + packets % args.sources)
                pcm = base_pcm if frames == chunk_frames else base_pcm[:frames * 2]
                accepted = pipeline.feed(AudioPacket(
                    uid, f"Generated source {uid}", origin + round(position * 1000 / 16000),
                    pcm, 16000, "generated-stream:1.0",
                ))
                refused_packets += not accepted
                packets += 1
                position += frames
                max_pending = max(max_pending, pipeline.pending_seconds)
                if packets % 100 == 0 or position == total_frames:
                    # Exercise exactly the bounded recent-page read used by UI.
                    recent = store.recent(500)
                    max_recent = max(max_recent, len(recent))
                    del recent
                    current_rss = rss_mb()
                    peak_rss = max(peak_rss, current_rss)
                    if warmup_rss is None:
                        warmup_rss = current_rss
                    samples.append({
                        "timeline_seconds": round(position / 16000, 3),
                        "real_elapsed_seconds": round(time.perf_counter() - start, 3),
                        "rss_mib": current_rss,
                        "pending_seconds": round(pipeline.pending_seconds, 3),
                        "sqlite_and_wal_bytes": disk_bytes(db_path),
                        "segments_published": pipeline.metrics["segments"],
                    })
            pipeline.stop()
            metrics = pipeline.metrics
            segment_count = sum(1 for _ in store.iter_segments())
            source_ids = sorted({segment.speaker_id for segment in store.iter_segments()})
            last_end = max((segment.end for segment in store.iter_segments()), default=0)
            gap_reasons = Counter(gap.reason for gap in store.iter_gaps())
            active_disk = disk_bytes(db_path)
            recent_count = len(store.recent(500))
        finally:
            pipeline.stop()
            store.close()
        final_database_bytes = db_path.stat().st_size
        gc.collect()
        final_rss = rss_mb()
        elapsed = time.perf_counter() - start
        cpu_end = process.cpu_times()

    cpu_seconds = cpu_end.user + cpu_end.system - cpu_start.user - cpu_start.system
    passed = all((
        not pipeline.failure, refused_packets == 0, metrics["dropped_seconds"] == 0,
        metrics["peak_pending_seconds"] <= 48.000001, max_pending <= 48.000001,
        metrics["pending_seconds"] == 0, segment_count > 0,
        recent_count <= 500, max_recent <= 500,
        final_database_bytes > initial_disk,
        final_rss - (warmup_rss or initial_rss) < 64,
        len(source_ids) == args.sources,
    ))
    report = {
        "kind": "accelerated_generated_pcm_fake_asr_soak",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "scope": {
            "real_zoom_meeting": False, "real_asr": False, "wall_clock_8_hours": False,
            "description": f"Accelerated 8-second generated PCM chunks, one active source at a time, rotating {args.sources} source IDs. Local SQLite persistence and recent(500) reads. No model, credentials, network, or personal transcript.",
            "limitation": "Empirical bounded-retention check; not an 8-hour wall-clock stability, Zoom networking, recognition accuracy, or GigaAM throughput measurement.",
        },
        "environment": {"os": platform.platform(), "python": platform.python_version(),
                        "logical_cpu_count": psutil.cpu_count()},
        "configuration": {"timeline_hours": args.hours, "sources": args.sources,
                          "pcm_rate_hz": 16000, "pcm_channels": 1,
                          "chunk_seconds": 8, "producer_wait_above_pending_seconds": 16,
                          "pipeline_pending_limit_seconds": 48, "recent_rows_limit": 500},
        "measurements": {
            "timeline_seconds": total_frames / 16000, "packets": packets,
            "real_runtime_seconds": round(elapsed, 3),
            "producer_pacing_wait_seconds": round(pacing_wait, 3),
            "process_cpu_seconds": round(cpu_seconds, 3),
            "process_cpu_percent_one_core": round(100 * cpu_seconds / elapsed, 2),
            "rss_initial_mib": initial_rss, "rss_after_first_100_packets_mib": warmup_rss,
            "rss_final_mib": final_rss, "rss_peak_observed_mib": peak_rss,
            "rss_delta_after_warmup_mib": round(final_rss - (warmup_rss or initial_rss), 3),
            "max_pending_observed_seconds": round(max_pending, 3),
            "max_pending_internal_seconds": round(metrics["peak_pending_seconds"], 3),
            "pending_after_stop_seconds": metrics["pending_seconds"],
            "refused_packets": refused_packets, "dropped_seconds": metrics["dropped_seconds"],
            "segments_saved": segment_count, "source_ids": source_ids,
            "last_segment_end_seconds": last_end,
            "max_recent_rows": max_recent, "recent_rows_after_stop": recent_count,
            "sqlite_initial_with_wal_bytes": initial_disk,
            "sqlite_final_with_wal_bytes": active_disk,
            "sqlite_final_checkpointed_bytes": final_database_bytes,
            "gap_reason_counts": dict(gap_reasons),
        },
        "pipeline_metrics_fake_asr_only": metrics,
        "event_counts": dict(events), "samples": samples,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": passed, **report["measurements"]}, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

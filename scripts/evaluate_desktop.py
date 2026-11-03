"""Real Windows playback -> WASAPI -> local GigaAM -> five text exports.

Only pre-approved, hash-verified public fixture WAVs are played. Microphone and
Zoom name observation stay disabled. Captured audio never touches disk. Close
other audio apps for this test: WASAPI receives the entire selected output mix.
This tests local hardware integration, NOT a real Zoom meeting or speaker IDs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from evaluate import Measurement, no_python_network, read_clip, score


def verify_exports(store, directory):
    from docx import Document
    from pypdf import PdfReader
    from gigaam_zoom.exports import EXPORT_FORMATS, export_transcript
    segments = list(store.iter_segments())
    result = []
    for kind in EXPORT_FORMATS:
        path = directory / ("public-transcript." + kind.lower())
        export_transcript(store, path, kind)
        if kind == "JSON":
            document = json.loads(path.read_text(encoding="utf-8"))
            values = document["segments"]
            valid = len(values) == len(segments) and all(row["attribution"] in {"unattributed_mix", "voice_cluster"} for row in values)
        else:
            if kind in {"TXT", "MD"}:
                text = path.read_text(encoding="utf-8")
            elif kind == "DOCX":
                text = "\n".join(paragraph.text for paragraph in Document(path).paragraphs)
            else:
                text = "\n".join(page.extract_text() for page in PdfReader(path).pages)
            # PDF line wrapping can split text; compare words across whitespace.
            import re
            compact = re.sub(r"\s+", " ", text)
            valid = all(all(word in compact for word in segment.text.split()) for segment in segments)
        if not valid:
            raise RuntimeError("Export round-trip validation failed: " + kind)
        result.append({"format": kind, "bytes": path.stat().st_size,
                       "parsed_successfully": True, "transcript_content_present": True,
                       "visual_layout_rechecked": False})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--diarization-model", type=Path,
                        help="Optional verified CAM++/WeSpeaker ONNX; all voices still anonymous local estimates")
    args = parser.parse_args()
    if args.report.exists():
        raise FileExistsError("Choose a new report file; evidence is not overwritten")
    import numpy as np
    import psutil
    import pyaudiowpatch as pa
    from gigaam_zoom.desktop import DesktopSettings
    from gigaam_zoom.desktop_audio import DesktopCapture
    from gigaam_zoom.desktop_attribution import DesktopSegmenter, Timeline
    from gigaam_zoom.engine import Transcriber, CODE_REVISION, MODEL_REVISION
    from gigaam_zoom.models import AudioPacket
    from gigaam_zoom.pipeline import Pipeline
    from gigaam_zoom.store import Store

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    clips = [(item, read_clip(item, args.manifest.parent)) for item in manifest["clips"]]
    if sum(len(pcm) for _, pcm in clips) > 16000 * 60:
        raise ValueError("Public fixtures must total at most 60 seconds")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    directory = args.work_dir / ("desktop-evaluation-" + uuid4().hex[:10])
    directory.mkdir()
    versions = {name: metadata.version(name) for name in
                ("PyAudioWPatch", "numpy", "torch", "torchaudio", "gigaam", "psutil")}
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Live public WAV playback through actual Windows WASAPI output loopback and local GigaAM",
        "live_zoom_meeting_tested": False, "microphone_tested": False,
        "zoom_speaker_names_tested": False, "simultaneous_speech_tested": False,
        "natural_code_switching_tested": False, "audio_files_written": 0,
        "diarization_enabled": args.diarization_model is not None,
        "speaker_attribution": ("anonymous local voice clusters + unknown; no true person names or Zoom IDs"
                                if args.diarization_model else "unattributed_mix; no synthetic person names or fake Zoom IDs"),
        "machine": {"os": platform.system(), "release": platform.release(),
                    "architecture": platform.machine(), "processor": platform.processor(),
                    "logical_cpus": os.cpu_count(), "python": platform.python_version(),
                    "physical_memory_mib": psutil.virtual_memory().total / 1048576},
        "versions": versions, "model_revision": MODEL_REVISION, "code_revision": CODE_REVISION,
        "implementation_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ("gigaam_zoom/desktop_audio.py", "gigaam_zoom/desktop_attribution.py",
                         "gigaam_zoom/engine.py", "gigaam_zoom/pipeline.py", "scripts/evaluate_desktop.py")},
        "measurement_limits": [
            "One run on a busy user PC; RSS is sampled every 50 ms, not a guaranteed maximum",
            "CPU machine percent divides process CPU time by logical CPU count",
            "Latency is relative to model-estimated last word time, not independent human alignment",
            "Only 52 reference words; no evidence of general meeting accuracy",
            "Output endpoint mixes all applications; test requires other audio apps quiet",
            "No Zoom/network/media-delivery or UI speaker matching validated by this test",
            "Wall real_time_factor includes paced playback, silence and startup; inference ratio is reported separately",
            "Voice-cluster coverage does not validate identity accuracy; the same 52 public words are a smoke test, not an independent benchmark",
        ],
    }
    engine = Transcriber(args.model_dir or DesktopSettings.load().model_dir)
    events, callbacks, played = [], [], []
    store = None
    capture = pipeline = None
    with no_python_network() as blocked:
        with Measurement() as model_load:
            engine.load()
        report["cold_model_load"] = model_load.result()
        diarization = None
        if args.diarization_model:
            from gigaam_zoom.diarization import SherpaSpeakerEmbedder, OnlineDiarizer, DiarizationTimeline
            with Measurement() as diarization_load:
                embedder = SherpaSpeakerEmbedder(args.diarization_model)
                embedder.load()
                diarization = DiarizationTimeline(OnlineDiarizer(embedder))
            report["diarization_model_load"] = diarization_load.result()
            report["diarization_model_sha256"] = embedder.model_spec.sha256
            report["diarization_model_id"] = embedder.model_spec.model_id
            report["diarization_threshold_profile"] = dict(zip(
                ("match_threshold", "new_threshold", "margin", "half_similarity"), embedder.model_spec.thresholds))
            report["versions"]["sherpa-onnx"] = metadata.version("sherpa-onnx")
            report["implementation_sha256"]["gigaam_zoom/diarization.py"] = hashlib.sha256((ROOT / "gigaam_zoom/diarization.py").read_bytes()).hexdigest()
        store = Store(directory / "public-transcript.sqlite")
        origin_ms = round(time.monotonic() * 1000)
        for key, value in {"origin_ms": origin_ms, "time_basis": "local_capture",
                "clock_origin_kind": "monotonic_capture_start", "capture_mode": "desktop_loopback",
                "audio_saved": False, "speaker_ids": "local_only_not_zoom_ids"}.items():
            store.set_metadata(key, value)
        def event(kind, payload):
            if kind == "segment":
                callbacks.append({"segment_id": payload.id,
                    "delay_after_last_word_seconds": time.monotonic() - origin_ms / 1000 - payload.end})
            elif kind not in {"progress", "capture_timestamp"}:
                events.append(kind)
        pipeline = Pipeline(store, engine, event, origin_ms=origin_ms,
                            max_pending_seconds=24, segmenter=DesktopSegmenter(Timeline(), diarization=diarization),
                            window_analyzer=diarization.analyze_window if diarization else None)
        def receive(source, pcm, stamp, rate):
            if source != "remote":
                raise RuntimeError("Microphone must remain disabled")
            pipeline.feed(AudioPacket("local:remote", "Public mixed source", stamp, pcm, rate, "public-desktop"))
        capture = DesktopCapture(on_audio=receive, on_event=lambda kind, payload: events.append(kind))
        try:
            pipeline.start()
            with Measurement() as processing:
                capture.start()
                with pa.PyAudio() as manager:
                    api = manager.get_host_api_info_by_type(pa.paWASAPI)
                    output = manager.get_device_info_by_index(int(api["defaultOutputDevice"]))
                    rate, channels = int(output["defaultSampleRate"]), int(output["maxOutputChannels"])
                    report["audio_format"] = {"render_sample_rate": rate, "render_channels": channels,
                                               "asr_sample_rate": 16000, "asr_channels": 1}
                    with manager.open(format=pa.paInt16, channels=channels, rate=rate,
                                      output=True, output_device_index=int(output["index"]),
                                      frames_per_buffer=rate // 50) as stream:
                        def play(values):
                            # Public fixture interpolation is playback-only;
                            # capture independently resamples with causal FIR.
                            count = round(len(values) * rate / 16000)
                            mono = np.interp(np.arange(count) * 16000 / rate,
                                             np.arange(len(values)), values)
                            stereo = np.repeat(np.rint(mono).astype("<i2")[:, None], channels, axis=1)
                            for start in range(0, len(stereo), rate // 50):
                                stream.write(stereo[start:start + rate // 50].tobytes())
                        play(np.zeros(16000, dtype="<i2"))
                        for item, pcm in clips:
                            start = time.monotonic() - origin_ms / 1000
                            play(pcm)
                            end = time.monotonic() - origin_ms / 1000
                            played.append({"item": item, "start": start, "end": end,
                                           "duration": len(pcm) / 16000})
                            play(np.zeros(24000, dtype="<i2"))
                        play(np.zeros(32000, dtype="<i2"))
                capture.stop()
                pipeline.stop()
            report["live_processing"] = processing.result(sum(len(pcm) / 16000 for _, pcm in clips))
            report["capture_metrics"] = capture.metrics
            report["pipeline_metrics"] = pipeline.metrics
            records = list(store.iter_segments())
            report["final_segment_count"] = len(records)
            report["gap_count"] = len(list(store.iter_gaps()))
            report["source_ids"] = sorted({row.speaker_id for row in records})
            report["attributions"] = sorted({row.attribution for row in records})
            if diarization:
                total_words = sum(len(row.text.split()) for row in records)
                attributed_words = sum(len(row.text.split()) for row in records if row.attribution == "voice_cluster")
                report["voice_grouping"] = {
                    "anonymous_cluster_count": diarization.diarizer.speaker_count,
                    "word_count": total_words, "words_with_cluster_estimate": attributed_words,
                    "word_coverage": attributed_words / max(1, total_words),
                    "true_identity_accuracy_validated": False,
                    "overlap_accuracy_validated": False,
                    "note": "Coverage measures labels emitted, not correctness; no named-person inference is made",
                }
            report["timestamps_valid"] = all(0 <= row.start < row.end <= processing.wall + 3 for row in records)
            # Match each final segment to its played public clip by its midpoint.
            report["samples"] = []
            for clip in played:
                assigned = [row for row in records if clip["start"] - .5 <= (row.start + row.end) / 2 <= clip["end"] + .5]
                hypothesis = " ".join(row.text for row in assigned)
                report["samples"].append({"fixture": {k: v for k, v in clip["item"].items() if k != "path"},
                    "duration_seconds": clip["duration"], "hypothesis": hypothesis,
                    **score(clip["item"]["reference"], hypothesis), "segment_count": len(assigned),
                    "speaker_label_word_counts": {source: sum(len(row.text.split()) for row in assigned if row.speaker_id == source)
                                                  for source in sorted({row.speaker_id for row in assigned})}})
            if diarization:
                cluster_languages = {}
                for sample in report["samples"]:
                    for source in sample["speaker_label_word_counts"]:
                        if source.startswith("local:voice:"):
                            cluster_languages.setdefault(source, []).append(sample["fixture"]["language"])
                report["voice_grouping"]["cluster_fixture_languages"] = cluster_languages
                report["voice_grouping"]["clusters_shared_across_different_fixture_languages"] = {
                    source: languages for source, languages in cluster_languages.items() if len(languages) > 1}
                report["voice_grouping"]["fixture_identity_basis"] = (
                    "The three clips are separate public recordings with distinct corpus/sample provenance; "
                    "human identities are not independently verified. Shared clusters expose a possible merge, "
                    "not a biometric identity determination.")
            hypothesis = " ".join(row.text for row in records)
            reference = " ".join(item["reference"] for item, _ in clips)
            report["sequential_multilingual"] = score(reference, hypothesis)
            report["result_after_last_word_seconds"] = [entry["delay_after_last_word_seconds"] for entry in callbacks]
            report["inference_seconds_per_fixture_second"] = pipeline.metrics["inference_seconds"] / sum(len(pcm) / 16000 for _, pcm in clips)
            report["exports"] = verify_exports(store, directory)
            report["event_kinds"] = sorted(set(events))
            report["blocked_python_network_attempts"] = len(blocked)
        finally:
            if capture:
                capture.stop()
            if pipeline:
                pipeline.stop()
            if store:
                store.close()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as out:
        json.dump(report, out, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"scope": report["scope"], "live_zoom_meeting_tested": False,
        "samples": [{"language": s["fixture"]["language"], "wer": s["wer"]} for s in report["samples"]],
        "metrics": report["live_processing"], "capture": report["capture_metrics"],
        "exports": report["exports"]}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()

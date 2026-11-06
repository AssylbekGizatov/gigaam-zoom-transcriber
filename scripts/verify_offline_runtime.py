"""Integration smoke: load real local weights with Python networking blocked.

Synthetic PCM verifies the inference path, not recognition quality, Windows
capture, corporate firewall configuration or a live Zoom meeting.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import socket
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=Path("models/gigaam"))
    parser.add_argument("--speaker-model", type=Path, default=Path("models/diarization/campplus.onnx"))
    args = parser.parse_args()
    attempts = []
    def blocked(*args, **kwargs):
        attempts.append("blocked")
        raise RuntimeError("Network attempt during offline inference")
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.create_connection = blocked
    urllib.request.urlopen = blocked
    import numpy as np
    from gigaam_zoom.engine import Transcriber
    from gigaam_zoom.diarization import SherpaSpeakerEmbedder
    samples = np.random.default_rng(2026).normal(0, .03, 3 * 16000).astype(np.float32)
    engine = Transcriber(args.model_dir)
    engine.load()
    words = engine.transcribe_words(samples, 16000)
    vector = SherpaSpeakerEmbedder(args.speaker_model)(samples)
    assert np.isfinite(vector).all() and vector.size > 0
    assert not attempts
    assert "huggingface_hub" not in sys.modules and "transformers" not in sys.modules
    print(json.dumps({
        "platform": sys.platform,
        "gigaam_version": importlib.metadata.version("gigaam"),
        "model_loaded": True, "synthetic_inference_completed": True,
        "words_from_synthetic_noise": len(words),
        "speaker_embedding_dimensions": int(vector.size),
        "network_attempts": len(attempts),
        "scope": "Real local weights; synthetic PCM; not speech-quality or live Zoom validation",
    }, indent=2))


if __name__ == "__main__":
    main()

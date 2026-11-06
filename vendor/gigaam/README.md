# Vendored GigaAM CPU inference

Source: https://github.com/salute-developers/GigaAM
Commit: `7447938d791c4f3e643386ee22c33777004293a5`
License: MIT, Copyright (c) 2024 GigaChat Team. Full terms in LICENSE.

This local package preserves the upstream Conformer, feature extraction, CTC
head, greedy decoding and word-timestamp computations for the pinned multilingual
character-CTC model. It is intentionally not the full upstream API.

Modifications:
- Inert package initialization: no model discovery, downloading or cache access.
- Keep tensor-only GigaAM/GigaAMASR initialization, forward and word decoding.
- Remove file loading, FFmpeg invocation, longform/VAD, emotion, RNNT and model
  ONNX-export convenience interfaces and unrelated datasets/download utilities.
- Character-only tokenizer rejects external tokenizer paths; no SentencePiece.
- Disable FlashAttention and its imports with explicit errors. CPU attention
  algorithms and encoder parameter names remain unchanged.
- Retain encoder auxiliary methods to minimize changes to its implementation.

Install from the project root through requirements-asr.txt. PyTorch and
Torchaudio CPU wheels are installed separately by project setup; Hydra and
OmegaConf are the only dependencies of this subset. No Git executable or model
hub client is required. The application verifies and loads its local model files.

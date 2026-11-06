# Derived from salute-developers/GigaAM, commit 7447938d791c4f3e643386ee22c33777004293a5.
# Copyright (c) 2024 GigaChat Team. MIT; see ../LICENSE and ../README.md.
from typing import List, Optional, Tuple
import hydra
import omegaconf
import torch
from torch import Tensor, nn
from .types import Word

class GigaAM(nn.Module):
    def __init__(self, cfg: omegaconf.DictConfig):
        super().__init__()
        self.cfg = cfg
        self.preprocessor = hydra.utils.instantiate(self.cfg.preprocessor)
        self.encoder = hydra.utils.instantiate(self.cfg.encoder)

    def forward(
        self, features: Tensor, feature_lengths: Tensor
    ) -> Tuple[Tensor, Tensor]:
        """
        Perform forward pass through the preprocessor and encoder.
        """
        features, feature_lengths = self.preprocessor(features, feature_lengths)
        if self._device.type == "cpu":
            return self.encoder(features, feature_lengths)
        with torch.autocast(device_type=self._device.type, dtype=torch.float16):
            return self.encoder(features, feature_lengths)

    @property
    def _device(self) -> torch.device:
        return next(self.parameters()).device


class GigaAMASR(GigaAM):
    def __init__(self, cfg: omegaconf.DictConfig):
        super().__init__(cfg)
        self.head = hydra.utils.instantiate(self.cfg.head)
        self.decoding = hydra.utils.instantiate(self.cfg.decoding)

    def _decode(
        self,
        encoded: Tensor,
        encoded_len: Tensor,
        wav_lens: Tensor,
        word_timestamps: bool = False,
    ) -> List[Tuple[str, Optional[List[Word]]]]:
        decoded = self.decoding.decode(self.head, encoded, encoded_len)
        if not word_timestamps:
            return [(t, None) for t, _, _ in decoded]
        from .timestamps_utils import compute_frame_shift, frames_to_words

        out: List[Tuple[str, Optional[List[Word]]]] = []
        for i, (text, token_ids, token_frames) in enumerate(decoded):
            frame_shift = compute_frame_shift(
                int(wav_lens[i].item()), int(encoded_len[i].item())
            )
            out.append(
                (
                    text,
                    frames_to_words(
                        self.decoding.tokenizer,
                        token_ids,
                        token_frames,
                        frame_shift,
                    ),
                )
            )
        return out

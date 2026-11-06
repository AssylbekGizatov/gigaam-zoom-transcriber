# Derived from salute-developers/GigaAM, commit 7447938d791c4f3e643386ee22c33777004293a5.
# Copyright (c) 2024 GigaChat Team. MIT; see ../LICENSE and ../README.md.
import torch
from torch import Tensor, nn

class CTCHead(nn.Module):
    """
    CTC Head module for Connectionist Temporal Classification.
    """

    def __init__(self, feat_in: int, num_classes: int):
        super().__init__()
        self.decoder_layers = torch.nn.Sequential(
            torch.nn.Conv1d(feat_in, num_classes, kernel_size=1)
        )

    def forward(self, encoder_output: Tensor) -> Tensor:
        return torch.nn.functional.log_softmax(
            self.decoder_layers(encoder_output).transpose(1, 2), dim=-1
        )

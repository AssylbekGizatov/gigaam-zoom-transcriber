# Derived from salute-developers/GigaAM, commit 7447938d791c4f3e643386ee22c33777004293a5.
# Copyright (c) 2024 GigaChat Team. MIT; see ../LICENSE and ../README.md.
from typing import Tuple
import torch
from torch import Tensor

def rtt_half(x: Tensor) -> Tensor:
    x1, x2 = x[..., : x.shape[-1] // 2], x[..., x.shape[-1] // 2 :]
    return torch.cat([-x2, x1], dim=x1.ndim - 1)

def apply_rotary_pos_emb(
    q: Tensor, k: Tensor, cos: Tensor, sin: Tensor, offset: int = 0
) -> Tuple[Tensor, Tensor]:
    """
    Applies Rotary Position Embeddings to query and key tensors.
    """
    cos, sin = (
        cos[offset : q.shape[0] + offset, ...],
        sin[offset : q.shape[0] + offset, ...],
    )
    cos = cos.to(dtype=q.dtype)
    sin = sin.to(dtype=q.dtype)
    return (q * cos) + (rtt_half(q) * sin), (k * cos) + (rtt_half(k) * sin)

# Derived from salute-developers/GigaAM, commit 7447938d791c4f3e643386ee22c33777004293a5.
# Copyright (c) 2024 GigaChat Team. MIT; see ../LICENSE and ../README.md.
from dataclasses import dataclass

@dataclass
class Word:
    text: str
    start: float
    end: float

"""Plain immutable contracts shared by the RTMS adapter, worker and exports."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class AudioPacket:
    user_id: str
    user_name: str | None
    timestamp_ms: int
    pcm: bytes  # Signed little-endian PCM16, mono; contains one Zoom source.
    sample_rate: int = 16000
    epoch: str = "0"  # A new transport generation after every reconnect.


@dataclass(frozen=True, slots=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class Segment:
    id: str
    speaker_id: str  # Real Zoom source ID, never a guessed account/person ID.
    display_name: str
    start: float
    end: float
    text: str
    status: Literal["preliminary", "final"] = "final"
    source_epoch: str = "0"


@dataclass(frozen=True, slots=True)
class Gap:
    speaker_id: str
    start: float
    end: float
    reason: str
    source_epoch: str = "0"

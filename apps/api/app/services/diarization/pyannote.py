"""Convert pyannote diarization output to API speaker turns."""

from typing import Any

from app.schemas.transcript import SpeakerTurn


def diarization_turns(result: Any) -> list[SpeakerTurn]:
    """Preserve all turns from pyannote 4 or legacy annotation output."""
    annotation = getattr(result, "speaker_diarization", result)
    turns = [
        SpeakerTurn(start=float(segment.start), end=float(segment.end), speaker=str(speaker))
        for segment, _, speaker in annotation.itertracks(yield_label=True)
    ]
    return sorted(turns, key=lambda turn: (turn.start, turn.end, turn.speaker))

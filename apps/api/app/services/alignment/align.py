"""Assign transcript regions to diarized speakers."""

from app.schemas.transcript import SpeakerTurn


def speaker_for_region(start: float, end: float, turns: list[SpeakerTurn]) -> str:
    """Return the speaker with the greatest overlap, or ``unknown``."""
    if not turns:
        return "unknown"

    def overlap(turn: SpeakerTurn) -> float:
        return max(0.0, min(end, turn.end) - max(start, turn.start))

    best_turn = max(turns, key=overlap)
    return best_turn.speaker if overlap(best_turn) > 0 else "unknown"

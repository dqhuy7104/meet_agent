"""Immutable source records shared by indexing and retrieval."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSegment:
    ordinal: int
    text: str
    speaker: str
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class MeetingSnapshot:
    meeting_id: str
    title: str
    revision: str
    segments: tuple[SourceSegment, ...]


@dataclass(frozen=True)
class SourceChunk:
    source_id: str
    meeting_id: str
    meeting_title: str
    revision: str
    ordinal: int
    text: str
    source_segments: tuple[SourceSegment, ...]
    chunk_revision: str = 'utf8-v1'
    segment_level_timing: bool = True

    @property
    def speaker(self) -> str:
        return ', '.join(dict.fromkeys(s.speaker for s in self.source_segments))

    @property
    def start_seconds(self) -> float:
        return min(s.start_seconds for s in self.source_segments)

    @property
    def end_seconds(self) -> float:
        return max(s.end_seconds for s in self.source_segments)


@dataclass(frozen=True)
class Candidate:
    chunk: SourceChunk
    cosine: float = 0.
    relevance_score: float = 0.

"""Deterministic UTF-8 windows retaining original segment timing."""
import hashlib
import json
from dataclasses import asdict, replace
from app.services.meetings.repository import MeetingDetail
from app.services.rag.types import MeetingSnapshot, SourceChunk, SourceSegment


def estimate_tokens(text: str) -> int:
    return (len(text.encode('utf-8')) + 2) // 3


def make_snapshot(meeting_id: str, title: str, segments: tuple[SourceSegment, ...]) -> MeetingSnapshot:
    canonical = json.dumps({'title': title, 'segments': [asdict(s) for s in segments]},
                           ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return MeetingSnapshot(meeting_id, title, hashlib.sha256(canonical.encode()).hexdigest(), segments)


def snapshot_meeting(detail: MeetingDetail) -> MeetingSnapshot:
    return make_snapshot(detail.meeting.id, detail.meeting.title or detail.meeting.original_filename, tuple(
        SourceSegment(i, s.text, s.speaker, s.start, s.end) for i, s in enumerate(detail.transcript_segments)))


def _split(segment: SourceSegment, max_bytes: int) -> list[SourceSegment]:
    pieces = []
    start = size = 0
    for index, character in enumerate(segment.text):
        width = len(character.encode('utf-8'))
        if size + width > max_bytes:
            pieces.append(replace(segment, text=segment.text[start:index]))
            start, size = index, 0
        size += width
    if start < len(segment.text):
        pieces.append(replace(segment, text=segment.text[start:]))
    return pieces


def chunk_transcript(snapshot: MeetingSnapshot, target_tokens: int = 400, overlap_tokens: int = 60) -> list[SourceChunk]:
    if target_tokens < 2 or not 0 <= overlap_tokens < target_tokens:
        raise ValueError('invalid_chunk_budget')
    # Small pieces allow overlap inside an oversized transcript region.
    piece_tokens = max(2, overlap_tokens or target_tokens)
    pieces = [piece for s in snapshot.segments if s.text.strip() for piece in _split(s, piece_tokens * 3)]
    chunks = []
    start = 0
    while start < len(pieces):
        end = start
        text = ''
        while end < len(pieces):
            separator = '\n' if end > start and pieces[end].ordinal != pieces[end - 1].ordinal else ''
            proposed = text + separator + pieces[end].text
            if end > start and estimate_tokens(proposed) > target_tokens:
                break
            text = proposed
            end += 1
        ordinal = len(chunks)
        source_id = f'{snapshot.meeting_id}:{snapshot.revision}:utf8-v1:{ordinal}'
        # Rejoin pieces from the same segment for exact metadata fidelity.
        contributing = []
        for piece in pieces[start:end]:
            if contributing and contributing[-1].ordinal == piece.ordinal:
                contributing[-1] = replace(contributing[-1], text=contributing[-1].text + piece.text)
            else:
                contributing.append(piece)
        chunks.append(SourceChunk(source_id, snapshot.meeting_id, snapshot.title, snapshot.revision,
                                  ordinal, text, tuple(contributing)))
        if end == len(pieces):
            break
        next_start = end
        overlap = 0
        while overlap_tokens and next_start > start + 1 and overlap < overlap_tokens:
            next_start -= 1
            overlap += estimate_tokens(pieces[next_start].text)
        start = max(start + 1, next_start)
    return chunks

"""Deterministic fusion and per-meeting evidence budgeting."""
import json
import re
from collections import defaultdict
from dataclasses import asdict, replace
from app.services.rag.chunking import estimate_tokens
from app.services.rag.types import Candidate, SourceChunk


def tokens(text: str) -> set[str]:
    return set(re.findall(r'[^\W_]+', text.casefold(), flags=re.UNICODE))


def rank_candidates(question: str, vector_hits: list[Candidate], text_hits: list[Candidate], min_cosine_similarity: float = .35) -> list[Candidate]:
    merged = {}
    fusion = defaultdict(float)
    for hits in (vector_hits, text_hits):
        seen = set()
        for rank, hit in enumerate(hits, 1):
            key = hit.chunk.source_id
            if key in seen:
                continue
            seen.add(key)
            merged[key] = hit
            fusion[key] += 1 / (60 + rank)
    query = tokens(question)
    eligible = []
    for key, hit in merged.items():
        coverage = len(query & tokens(hit.chunk.text)) / max(1, len(query))
        if coverage or hit.cosine >= min_cosine_similarity:
            eligible.append((hit, coverage, fusion[key]))
    eligible.sort(key=lambda row: (-row[2], row[0].chunk.meeting_id, row[0].chunk.source_id))
    # Seed every meeting with its best fused chunk before spending remaining slots.
    best = {}
    for row in eligible:
        best.setdefault(row[0].chunk.meeting_id, row)
    seed = list(best.values())[:40]
    keys = {row[0].chunk.source_id for row in seed}
    shortlisted = seed + [row for row in eligible if row[0].chunk.source_id not in keys][:40-len(seed)]
    ranked = [replace(hit, relevance_score=.7 * max(0., min(1., hit.cosine)) + .3 * coverage)
              for hit, coverage, _ in shortlisted]
    return sorted(ranked, key=lambda h: (-h.relevance_score, h.chunk.meeting_id, h.chunk.source_id))


def context_tokens(chunk: SourceChunk) -> int:
    return estimate_tokens(json.dumps(asdict(chunk), ensure_ascii=False))


def select_context(candidates: list[Candidate], k: int, token_budget: int) -> list[SourceChunk]:
    grouped = {}
    for candidate in candidates:
        grouped.setdefault(candidate.chunk.meeting_id, []).append(candidate)
    meetings = sorted(grouped, key=lambda m: (-max(c.relevance_score for c in grouped[m]), m))[:k]
    context = []
    for ordinal in range(5):
        for meeting in meetings:
            if ordinal >= len(grouped[meeting]):
                continue
            chunk = grouped[meeting][ordinal].chunk
            cost = context_tokens(chunk)
            if cost <= token_budget:
                context.append(chunk)
                token_budget -= cost
    return context

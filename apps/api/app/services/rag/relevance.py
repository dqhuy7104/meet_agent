"""Bounded evidence selection before generation, using the selected chat model."""
import hashlib
import json
from pathlib import Path

from pydantic import Field, ValidationError

from app.core.rag_config import ChatSelection, RagError, StrictModel
from app.services.rag.chunking import estimate_tokens
from app.services.rag.providers import ModelClient
from app.services.rag.ranking import select_context
from app.services.rag.types import Candidate, SourceChunk

FILTER_VERSION = 'relevance_filter_v1'
FILTER_PROMPT = (Path(__file__).parent / 'prompts/relevance_filter_v1.txt').read_text()
FILTER_HASH = hashlib.sha256(FILTER_PROMPT.encode()).hexdigest()


class RelevantSources(StrictModel):
    source_ids: list[str] = Field(max_length=40)


def build_relevance_messages(question: str, sources: list[SourceChunk]) -> list[dict[str, str]]:
    return [{'role':'system', 'content':FILTER_PROMPT},
            {'role':'user', 'content':json.dumps({'question':question, 'sources':[
                {'source_id':f'source_{i+1}', 'meeting_title':s.meeting_title, 'text':s.text,
                 'speaker':s.speaker, 'start_seconds':s.start_seconds, 'end_seconds':s.end_seconds}
                for i, s in enumerate(sources)]}, ensure_ascii=False)}]


def select_relevance_context(question: str, candidates: list[Candidate], selection: ChatSelection) -> list[SourceChunk]:
    overhead = sum(estimate_tokens(m['content'])+16 for m in build_relevance_messages(question, []))
    budget = selection.model.context_tokens - selection.model.output_tokens - overhead - 256
    if budget < 0:
        raise RagError('prompt_too_long', 422)
    context = select_context(candidates, 40, budget)
    sources = []
    for source in context:
        output_tokens = estimate_tokens(json.dumps({'source_ids':[f'source_{i+1}' for i in range(len(sources)+1)]})) + 16
        if output_tokens <= selection.model.output_tokens:
            sources.append(source)
    return sources


async def filter_sources(question: str, sources: list[SourceChunk], selection: ChatSelection,
                         client: ModelClient) -> list[SourceChunk]:
    if not sources:
        return []
    raw = await client.generate(selection, build_relevance_messages(question, sources))
    try:
        selected = RelevantSources.model_validate_json(raw).source_ids
    except (ValueError, ValidationError):
        return []
    known = {f'source_{i+1}' for i in range(len(sources))}
    if len(set(selected)) != len(selected) or any(source_id not in known for source_id in selected):
        return []
    accepted = set(selected)
    return [source for i, source in enumerate(sources) if f'source_{i+1}' in accepted]

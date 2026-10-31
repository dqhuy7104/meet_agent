"""Mechanical citation validation; semantic support needs separate evaluation."""
import json
from typing import Literal
from pydantic import Field, ValidationError
from app.core.rag_config import ChatSelection, StrictModel
from app.schemas.chat import Citation, RagAnswer
from app.services.rag.prompt import build_messages
from app.services.rag.providers import ModelClient
from app.services.rag.types import SourceChunk


class Claim(StrictModel):
    text: str = Field(min_length=1, max_length=10000)
    citation_ids: list[str] = Field(min_length=1, max_length=100)


class Generation(StrictModel):
    status: Literal['answered', 'insufficient', 'conflicting']
    claims: list[Claim] = Field(max_length=100)


def insufficient(prompt_version: str) -> RagAnswer:
    return RagAnswer(answer='Chưa có đủ bằng chứng nhất quán trong các cuộc họp để trả lời.',
                     retrieval_status='insufficient_evidence', prompt_version=prompt_version)


async def generate_answer(question: str, sources: list[SourceChunk], selection: ChatSelection,
                          client: ModelClient, prompt_version: str, system_prompt: str = '') -> RagAnswer:
    if not sources:
        return insufficient(prompt_version)
    raw = await client.generate(selection, build_messages(question, sources, prompt_version, system_prompt))
    try:
        generation = Generation.model_validate_json(raw)
    except (ValueError, ValidationError):
        return insufficient(prompt_version)
    if generation.status != 'answered' or not generation.claims:
        return insufficient(prompt_version)
    known = {s.source_id: s for s in sources}
    used = []
    lines = []
    for claim in generation.claims:
        if not claim.text.strip() or '[' in claim.text or ']' in claim.text or any(i in claim.text for i in known):
            return insufficient(prompt_version)
        if len(set(claim.citation_ids)) != len(claim.citation_ids) or any(i not in known for i in claim.citation_ids):
            return insufficient(prompt_version)
        lines.append(claim.text.strip())
        used.extend(i for i in claim.citation_ids if i not in used)
    citations = [Citation(source_id=i, meeting_id=known[i].meeting_id, meeting_title=known[i].meeting_title,
                          speaker=known[i].speaker, start_seconds=known[i].start_seconds,
                          end_seconds=known[i].end_seconds, excerpt=known[i].text) for i in used]
    return RagAnswer(answer='\n\n'.join(lines), retrieval_status='answered', prompt_version=prompt_version, citations=citations)

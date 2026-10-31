import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from app.core.rag_config import ChatSelection, RagError, StrictModel
from app.services.rag.chunking import estimate_tokens

CONTEXT_VERSION = 'context_resolution_v1'
CONTEXT_PROMPT = (Path(__file__).parent/'prompts/context_resolution_v1.txt').read_text()
CONTEXT_HASH = hashlib.sha256(CONTEXT_PROMPT.encode()).hexdigest()


class Resolution(StrictModel):
    status: Literal['resolved','clarification']
    question: str | None = Field(max_length=2000)

    @field_validator('question')
    @classmethod
    def clean_question(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value or '\0' in value:
                raise ValueError('invalid_question')
        return value

    @model_validator(mode='after')
    def validate_resolution(self):
        if (self.status == 'resolved') != (self.question is not None):
            raise ValueError('invalid_resolution')
        return self


def context_messages(question: str, history: list[dict]) -> list[dict[str,str]]:
    return [{'role':'system','content':CONTEXT_PROMPT},
            {'role':'user','content':json.dumps({'question':question,'history':history},ensure_ascii=False)}]


def select_history(question: str, newest_pairs: list[dict], selection: ChatSelection, prompt: str = CONTEXT_PROMPT) -> list[dict]:
    overhead = estimate_tokens(prompt)+estimate_tokens(context_messages(question,[])[1]['content'])+32
    budget = min(2000, selection.model.context_tokens-selection.model.output_tokens-overhead-256)
    if budget < 0:
        raise RagError('prompt_too_long',422)
    selected = []
    for pair in newest_pairs[:6]:
        item = {'question':pair['question'],'answer':pair['answer']}
        cost = estimate_tokens(json.dumps(item,ensure_ascii=False))+2
        if cost > budget:
            break
        selected.append(item)
        budget -= cost
    return list(reversed(selected))


async def resolve_question(question: str, history: list[dict], selection: ChatSelection, client) -> str | None:
    raw = await client.generate(selection, context_messages(question,history))
    try:
        return Resolution.model_validate_json(raw).question
    except (ValueError,ValidationError):
        raise RagError('invalid_context_response') from None

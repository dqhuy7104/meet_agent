import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from app.core.rag_config import ChatSelection, RagError, StrictModel
from app.schemas.chat import AnswerPart, Citation, RagAnswer
from app.services.rag.chunking import estimate_tokens
from app.services.rag.types import SourceChunk

ROUTING_PROMPT = (Path(__file__).parent/'prompts/chat_routing_v1.txt').read_text()
ANSWER_PROMPT = (Path(__file__).parent/'prompts/chat_answer_v1.txt').read_text()
ROUTING_HASH = hashlib.sha256(ROUTING_PROMPT.encode()).hexdigest()
ANSWER_HASH = hashlib.sha256(ANSWER_PROMPT.encode()).hexdigest()


class RouteDecision(StrictModel):
    intent: Literal['conversation','clarification','transform','retrieval']
    question: str | None = Field(default=None, min_length=1, max_length=2000)
    target_turn: StrictInt | None = Field(default=None,ge=1,le=6)

    @model_validator(mode='after')
    def validate_question(self):
        if self.intent in {'retrieval','transform'} and (not self.question or not self.question.strip() or '\0' in self.question):
            raise ValueError('question_required')
        if self.intent == 'transform' and self.target_turn is None:
            raise ValueError('target_required')
        return self


class ChatGeneration(StrictModel):
    parts: list[AnswerPart] = Field(min_length=1,max_length=100)


def messages(prompt: str, data: dict, selection: ChatSelection) -> list[dict[str,str]]:
    result = [{'role':'system','content':prompt},{'role':'user','content':json.dumps(data,ensure_ascii=False)}]
    if sum(estimate_tokens(message['content'])+16 for message in result)+selection.model.output_tokens+256 > selection.model.context_tokens:
        raise RagError('prompt_too_long',422)
    return result


async def route_message(question: str, history: list[dict], selection: ChatSelection, client) -> RouteDecision:
    raw = await client.generate(selection,messages(ROUTING_PROMPT,{'question':question,'history':history},selection))
    try:
        decision = RouteDecision.model_validate_json(raw)
        if decision.intent == 'transform' and decision.target_turn > len(history):
            raise ValueError('invalid_target')
        return decision
    except ValueError:
        raise RagError('invalid_chat_route') from None


def source_budget(question: str, history: list[dict], allow_model_knowledge: bool, selection: ChatSelection,
                  system_prompt: str = '', intent: str = 'retrieval') -> int:
    prompt = system_prompt+'\n\n'+ANSWER_PROMPT if system_prompt else ANSWER_PROMPT
    data = {'question':question,'history':history,'sources':[],
            'allow_model_knowledge':allow_model_knowledge,'intent':intent}
    overhead = estimate_tokens(prompt)+estimate_tokens(json.dumps(data,ensure_ascii=False))+32
    budget = selection.model.context_tokens-selection.model.output_tokens-overhead-256
    if budget < 0:
        raise RagError('prompt_too_long',422)
    return budget


async def generate_chat_answer(question: str, history: list[dict], sources: list[SourceChunk],
                               allow_model_knowledge: bool, selection: ChatSelection, client,
                               system_prompt: str = '', intent: str = 'retrieval') -> RagAnswer:
    prompt = system_prompt+'\n\n'+ANSWER_PROMPT if system_prompt else ANSWER_PROMPT
    raw = await client.generate(selection,messages(prompt,{'question':question,'history':history,
        'sources':[asdict(source) for source in sources],'allow_model_knowledge':allow_model_knowledge,'intent':intent},selection))
    try:
        generation = ChatGeneration.model_validate_json(raw)
        known = {source.source_id:source for source in sources}
        used = []
        for part in generation.parts:
            if not part.text.strip() or '[' in part.text or ']' in part.text or any(source_id in part.text for source_id in known):
                raise ValueError()
            if part.kind == 'meeting':
                if not part.citation_ids or len(set(part.citation_ids)) != len(part.citation_ids) or any(source_id not in known for source_id in part.citation_ids):
                    raise ValueError()
                used.extend(source_id for source_id in part.citation_ids if source_id not in used)
            elif part.citation_ids or (part.kind == 'model' and not allow_model_knowledge):
                raise ValueError()
        citations = [Citation(source_id=source_id,meeting_id=known[source_id].meeting_id,meeting_title=known[source_id].meeting_title,
            speaker=known[source_id].speaker,start_seconds=known[source_id].start_seconds,end_seconds=known[source_id].end_seconds,
            excerpt=known[source_id].text) for source_id in used]
        return RagAnswer(answer='\n\n'.join(part.text.strip() for part in generation.parts),
            retrieval_status='answered' if used or any(part.kind=='model' for part in generation.parts) or intent=='conversation' else 'insufficient_evidence',
            prompt_version='chat_answer_v1',citations=citations,parts=generation.parts)
    except ValueError:
        raise RagError('invalid_chat_response') from None

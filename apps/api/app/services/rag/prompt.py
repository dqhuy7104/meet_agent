"""Versioned instructions isolated from untrusted transcript data."""
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from app.core.rag_config import RagError
from app.services.rag.types import SourceChunk

PROMPT_VERSION = 'meeting_qa_v1'
PROMPT_TEXT = (Path(__file__).parent / 'prompts/meeting_qa_v1.txt').read_text()
PROMPT_HASH = hashlib.sha256(PROMPT_TEXT.encode()).hexdigest()


def build_messages(question: str, sources: list[SourceChunk], prompt_version: str) -> list[dict[str, str]]:
    if prompt_version != PROMPT_VERSION:
        raise RagError('unknown_prompt_version')
    return [{'role': 'system', 'content': PROMPT_TEXT},
            {'role': 'user', 'content': json.dumps({'question': question, 'sources': [asdict(s) for s in sources]}, ensure_ascii=False)}]

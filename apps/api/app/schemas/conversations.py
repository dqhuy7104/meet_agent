from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictInt, field_validator

from app.core.rag_config import StrictModel
from app.schemas.chat import ConversationConfig, RagAnswer, RagQuery


class ConversationCreate(StrictModel):
    project_id: str | None = Field(default=None, min_length=1, max_length=200)


class TurnRequest(StrictModel):
    question: str = Field(min_length=1, max_length=2000)
    request_id: UUID
    expected_last_ordinal: StrictInt = Field(ge=0)
    config: ConversationConfig

    @field_validator('question', mode='before')
    @classmethod
    def clean_question(cls, value: str) -> str:
        return RagQuery.clean_question(value)


class ConversationSummary(StrictModel):
    id: str
    title: str
    creator_account_id: str
    creator_email: str | None = None
    project_id: str | None
    config: ConversationConfig | None
    created_at: datetime
    updated_at: datetime
    last_ordinal: int
    read_only: bool


class ConversationTurn(StrictModel):
    id: str
    ordinal: int
    question: str | None
    config: ConversationConfig | None
    status: Literal['running','completed','failed','interrupted','redacted','stale']
    answer: RagAnswer | None
    failure_code: str | None = None
    created_at: datetime
    finished_at: datetime | None


class ConversationList(StrictModel):
    items: list[ConversationSummary]
    next_cursor: str | None


class ConversationDetail(StrictModel):
    conversation: ConversationSummary
    turns: list[ConversationTurn]
    next_before_ordinal: int | None

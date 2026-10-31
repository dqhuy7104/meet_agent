"""Public meeting question-answering contracts."""
from typing import Annotated, Literal
from pydantic import Field, StrictInt, StrictFloat, StrictBool, field_validator
from app.core.rag_config import StrictModel, EmbeddingProfile, ChatModel


class RetrievalConfig(StrictModel):
    k: Annotated[StrictInt, Field(ge=1)]
    chat_provider_id: str = Field(min_length=1, max_length=200)
    chat_model_id: str = Field(min_length=1, max_length=200)
    embedding_profile_id: str = Field(min_length=1, max_length=200)
    context_weight: Annotated[StrictFloat, Field(ge=0, le=1, allow_inf_nan=False)] = .8


class ConversationConfig(RetrievalConfig):
    allow_model_knowledge: StrictBool = False


class RagQuery(RetrievalConfig):
    question: str = Field(min_length=1, max_length=2000)
    project_id: str | None = Field(default=None, min_length=1, max_length=200)
    meeting_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(default=None, max_length=100)

    @field_validator('question', mode='before')
    @classmethod
    def clean_question(cls, value: str) -> str:
        if type(value) is not str or '\0' in value:
            raise ValueError('invalid_question')
        return value.strip()

    @field_validator('meeting_ids')
    @classmethod
    def unique_meetings(cls, value):
        return list(dict.fromkeys(value)) if value is not None else None


class Citation(StrictModel):
    source_id: str
    meeting_id: str
    meeting_title: str
    speaker: str
    start_seconds: float
    end_seconds: float
    excerpt: str


class SelectedMeeting(StrictModel):
    meeting_id: str
    title: str
    relevance_score: float


class AnswerPart(StrictModel):
    kind: Literal['meeting', 'model', 'conversation']
    text: str = Field(min_length=1, max_length=10000)
    citation_ids: list[str] = Field(default_factory=list, max_length=100)


class RagAnswer(StrictModel):
    answer: str
    retrieval_status: Literal['answered', 'insufficient_evidence']
    prompt_version: str
    selected_meetings: list[SelectedMeeting] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    parts: list[AnswerPart] = Field(default_factory=list, max_length=100)


class MeetingSummary(StrictModel):
    summary: str
    provider_id: str
    model_id: str
    prompt_version: str


class ProviderChoice(StrictModel):
    id: str
    name: str
    is_external: bool
    chat_models: list[str]


class ProfileChoice(StrictModel):
    id: str
    provider_id: str
    model: str
    revision: str


class ProviderCatalog(StrictModel):
    max_k: int
    providers: list[ProviderChoice]
    embedding_profiles: list[ProfileChoice]


class MeetingIndexStatus(StrictModel):
    meeting_id: str
    title: str
    state: Literal['ready', 'pending', 'running', 'failed', 'stale']
    attempts: int = 0
    failure_code: str | None = None


class ProfileIndexStatus(StrictModel):
    profile_id: str
    configuration_status: Literal['valid', 'not_registered', 'changed'] = 'valid'
    meetings: list[MeetingIndexStatus] = Field(default_factory=list)


class IndexStatus(StrictModel):
    profiles: list[ProfileIndexStatus]


class EvaluationState(StrictModel):
    meeting_revisions: dict[str, str]
    embedding_profile: EmbeddingProfile
    prompt_version: str
    prompt_hash: str
    chat_provider_id: str
    chat_model: ChatModel
    min_cosine_similarity: float
    retrieval_version: Literal['hybrid-v1', 'lexical-v2'] = 'hybrid-v1'
    ranking_weights: dict[str, float] = Field(default_factory=dict)
    relevance_filter_version: str = ''
    relevance_filter_hash: str = ''
    provider_versions: str
    device: str

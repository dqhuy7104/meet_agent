"""Public control-plane contracts for local Meet recording."""

from pydantic import BaseModel, Field


class BotSessionCreate(BaseModel):
    project_id: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=200)


class BotSessionResponse(BaseModel):
    project_id: str | None = None
    id: str
    status: str
    failure_code: str | None
    created_at: str
    started_at: str | None
    ended_at: str | None
    finalized_part_count: int
    capture_incomplete: bool
    meeting_id: str | None
    stop_requested: bool

"""Workspace instructions, separate from fixed output and evidence contracts."""
from pydantic import Field, field_validator
from app.core.rag_config import StrictModel


class SystemPrompts(StrictModel):
    summary_system_prompt: str = Field(default='', max_length=8000)
    rag_system_prompt: str = Field(default='', max_length=8000)

    @field_validator('summary_system_prompt', 'rag_system_prompt')
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        if '\0' in value:
            raise ValueError('Prompt must not contain null characters')
        return value.strip()

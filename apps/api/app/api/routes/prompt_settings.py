"""Administrator-only workspace system prompt configuration."""
from typing import Annotated
from fastapi import APIRouter, Depends
from app.api.auth import get_session_repository
from app.api.routes.projects import AccountDependency, WriteDependencies, require_admin
from app.schemas.system_prompts import SystemPrompts
from app.services.access.sessions import SessionRepository
from app.services.llm.prompt_settings import PromptRepository

router = APIRouter(prefix='/admin/system-prompts', tags=['administration'])


def get_prompt_repository(session: Annotated[SessionRepository, Depends(get_session_repository)]) -> PromptRepository:
    return PromptRepository(session.connection)


RepositoryDependency = Annotated[PromptRepository, Depends(get_prompt_repository)]


@router.get('', response_model=SystemPrompts)
def get_prompts(account: AccountDependency, repository: RepositoryDependency) -> SystemPrompts:
    require_admin(account)
    return repository.get()


@router.put('', response_model=SystemPrompts, dependencies=WriteDependencies)
def save_prompts(payload: SystemPrompts, account: AccountDependency, repository: RepositoryDependency) -> SystemPrompts:
    require_admin(account)
    return repository.save(payload)

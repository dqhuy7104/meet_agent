"""Public project and account administration contracts."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

ProjectRole = Literal['project_manager', 'member', 'viewer']

class ProjectName(BaseModel):
    name: str = Field(min_length=1, max_length=200, pattern=r'\S')

class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    is_archived: bool

class MembershipChange(BaseModel):
    role: ProjectRole

class MembershipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    project_id: str
    account_id: str
    role: ProjectRole

class AccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    is_system_admin: bool
    is_disabled: bool

class AccountChange(BaseModel):
    is_system_admin: bool | None = None
    is_disabled: bool | None = None

"""Project and meeting authorization; invisible resources always return 404."""
from typing import Literal
from fastapi import HTTPException
from app.services.access.repository import Account, AccessRepository, Project
from app.services.meetings.repository import MeetingRecord, MeetingRepository

ProjectAction = Literal['read', 'manage_members', 'upload', 'start_bot', 'rename', 'archive', 'restore']
MeetingAction = Literal['read', 'download', 'chat', 'edit', 'delete', 'cancel', 'retry', 'identify']


def visible_project_ids(account: Account, repository: AccessRepository) -> list[str]:
    return [project.id for project in repository.visible_projects(account)]


def require_project(account: Account, project_id: str, action: ProjectAction,
                    repository: AccessRepository) -> Project:
    role = repository.role(account, project_id)
    project = repository.project(project_id) if role else None
    if project is None:
        raise HTTPException(404, 'Project not found')
    if action == 'read':
        return project
    if action in {'upload', 'start_bot'}:
        allowed = role in {'system_admin', 'project_manager', 'member'} and not project.is_archived
    elif action == 'manage_members':
        allowed = role in {'system_admin', 'project_manager'}
    else:
        allowed = role == 'system_admin' and action in {'rename', 'archive', 'restore'}
    if not allowed:
        raise HTTPException(403, 'Action not permitted')
    return project


def require_meeting(account: Account, meeting_id: str, action: MeetingAction,
                    repository: AccessRepository) -> MeetingRecord:
    meeting = MeetingRepository(repository.connection).get_meeting(meeting_id)
    if meeting is None or account.is_disabled:
        raise HTTPException(404, 'Meeting not found')
    if meeting.project_id is None:
        raise HTTPException(404, 'Meeting not found')
    require_project(account, meeting.project_id, 'read', repository)
    role = repository.role(account, meeting.project_id)
    allowed = action in {'read', 'download', 'chat'} or role in {'system_admin', 'project_manager'}
    if action in {'delete', 'cancel', 'retry'} and role == 'member':
        allowed = meeting.source == 'upload' and meeting.uploaded_by_account_id == account.id
    if not allowed:
        raise HTTPException(403, 'Action not permitted')
    return meeting

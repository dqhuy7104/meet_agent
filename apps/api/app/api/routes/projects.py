"""Project discovery and scoped membership administration."""
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Response
from app.api.auth import CurrentAccount, require_account, require_csrf, get_session_repository
from app.services.access.sessions import SessionRepository
from app.services.access.repository import AccessRepository
from app.services.access.policy import require_project
from app.schemas.project import ProjectName, ProjectResponse, MembershipChange, MembershipResponse, AccountResponse

router = APIRouter(prefix='/projects', tags=['projects'])
AccountDependency = Annotated[CurrentAccount, Depends(require_account)]


def get_access_repository(session: Annotated[SessionRepository, Depends(get_session_repository)]) -> AccessRepository:
    return AccessRepository(session.connection)

RepositoryDependency = Annotated[AccessRepository, Depends(get_access_repository)]
WriteDependencies = [Depends(require_csrf)]

def project_response(project, account: CurrentAccount, repository: AccessRepository) -> ProjectResponse:
    return ProjectResponse(id=project.id, name=project.name, is_archived=project.is_archived,
                           role=repository.role(account, project.id))



def require_admin(account: CurrentAccount) -> None:
    if account.is_disabled or not account.is_system_admin:
        raise HTTPException(403, 'System administrator required')


@router.get('', response_model=list[ProjectResponse])
def projects(account: AccountDependency, repository: RepositoryDependency) -> list[ProjectResponse]:
    return [project_response(project, account, repository) for project in repository.visible_projects(account)]


@router.post('', response_model=ProjectResponse, status_code=201, dependencies=WriteDependencies)
def create_project(payload: ProjectName, account: AccountDependency, repository: RepositoryDependency) -> ProjectResponse:
    require_admin(account)
    return project_response(repository.create_project(payload.name), account, repository)


@router.get('/{project_id}', response_model=ProjectResponse)
def project(project_id: str, account: AccountDependency, repository: RepositoryDependency) -> ProjectResponse:
    return project_response(require_project(account, project_id, 'read', repository), account, repository)


@router.patch('/{project_id}', response_model=ProjectResponse, dependencies=WriteDependencies)
def rename_project(project_id: str, payload: ProjectName, account: AccountDependency, repository: RepositoryDependency) -> ProjectResponse:
    require_project(account, project_id, 'rename', repository)
    return project_response(repository.update_project(project_id, name=payload.name), account, repository)


@router.post('/{project_id}/archive', response_model=ProjectResponse, dependencies=WriteDependencies)
def archive_project(project_id: str, account: AccountDependency, repository: RepositoryDependency) -> ProjectResponse:
    require_project(account, project_id, 'archive', repository)
    return project_response(repository.update_project(project_id, is_archived=True), account, repository)


@router.post('/{project_id}/restore', response_model=ProjectResponse, dependencies=WriteDependencies)
def restore_project(project_id: str, account: AccountDependency, repository: RepositoryDependency) -> ProjectResponse:
    require_project(account, project_id, 'restore', repository)
    return project_response(repository.update_project(project_id, is_archived=False), account, repository)


@router.get('/{project_id}/members', response_model=list[MembershipResponse])
def members(project_id: str, account: AccountDependency, repository: RepositoryDependency) -> list[MembershipResponse]:
    require_project(account, project_id, 'manage_members', repository)
    return repository.members(project_id)


@router.get('/{project_id}/accounts/lookup', response_model=AccountResponse)
def lookup_account(project_id: str, email: str, account: AccountDependency, repository: RepositoryDependency) -> AccountResponse:
    require_project(account, project_id, 'manage_members', repository)
    target = repository.account_by_email(email)
    if target is None:
        raise HTTPException(404, 'Account not found')
    return target


@router.get('/{project_id}/members/{account_id}', response_model=MembershipResponse)
def member(project_id: str, account_id: str, account: AccountDependency, repository: RepositoryDependency) -> MembershipResponse:
    require_project(account, project_id, 'manage_members', repository)
    membership = next((item for item in repository.members(project_id) if item.account_id == account_id), None)
    if membership is None:
        raise HTTPException(404, 'Membership not found')
    return membership


def authorize_membership_change(account: CurrentAccount, project_id: str, account_id: str,
                                role: str | None, repository: AccessRepository) -> None:
    require_project(account, project_id, 'manage_members', repository)
    target = repository.account_by_id(account_id)
    if target is None:
        raise HTTPException(404, 'Account not found')
    if not account.is_system_admin and (role == 'project_manager' or repository.role(target, project_id) in {'project_manager', 'system_admin'}):
        raise HTTPException(403, 'Only system administrators manage managers')


@router.put('/{project_id}/members/{account_id}', response_model=MembershipResponse, dependencies=WriteDependencies)
def set_member(project_id: str, account_id: str, payload: MembershipChange, account: AccountDependency, repository: RepositoryDependency) -> MembershipResponse:
    with repository.connection.transaction():
        repository.connection.execute('SELECT id FROM projects WHERE id=%s FOR UPDATE', (project_id,))
        authorize_membership_change(account, project_id, account_id, payload.role, repository)
        try:
            return repository.set_membership(project_id, account_id, payload.role)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None



@router.delete('/{project_id}/members/{account_id}', status_code=204, dependencies=WriteDependencies)
def remove_member(project_id: str, account_id: str, account: AccountDependency, repository: RepositoryDependency) -> Response:
    with repository.connection.transaction():
        repository.connection.execute('SELECT id FROM projects WHERE id=%s FOR UPDATE', (project_id,))
        authorize_membership_change(account, project_id, account_id, None, repository)
        try:
            repository.remove_membership(project_id, account_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
    return Response(status_code=204)

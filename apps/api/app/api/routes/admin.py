"""System administrator account and manager operations."""
from fastapi import APIRouter, HTTPException
from app.api.routes.projects import AccountDependency, RepositoryDependency, WriteDependencies, require_admin
from app.schemas.project import AccountResponse, AccountChange, MembershipResponse
from app.services.access.policy import require_project

router = APIRouter(prefix='/admin', tags=['administration'])


@router.get('/accounts', response_model=list[AccountResponse])
def accounts(account: AccountDependency, repository: RepositoryDependency) -> list[AccountResponse]:
    require_admin(account)
    return repository.accounts()


@router.patch('/accounts/{account_id}', response_model=AccountResponse, dependencies=WriteDependencies)
def update_account(account_id: str, payload: AccountChange, account: AccountDependency, repository: RepositoryDependency) -> AccountResponse:
    require_admin(account)
    try:
        target = repository.update_account(account_id, **payload.model_dump())
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    if target is None:
        raise HTTPException(404, 'Account not found')
    return target


@router.put('/projects/{project_id}/managers/{account_id}', response_model=MembershipResponse, dependencies=WriteDependencies)
def appoint_manager(project_id: str, account_id: str, account: AccountDependency, repository: RepositoryDependency) -> MembershipResponse:
    require_admin(account)
    require_project(account, project_id, 'manage_members', repository)
    if repository.account_by_id(account_id) is None:
        raise HTTPException(404, 'Account not found')
    return repository.set_membership(project_id, account_id, 'project_manager')

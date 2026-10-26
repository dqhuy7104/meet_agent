"""Authentication dependencies shared by protected HTTP endpoints."""
from collections.abc import Iterator
from typing import Annotated
import secrets

from fastapi import Depends, HTTPException, Request
from app.api.local_access import require_loopback
from app.core.database_config import get_database_settings
from app.db.session import open_database, validate_database
from app.services.access.repository import Account
from app.services.access.sessions import COOKIE_NAME, SessionRepository

CurrentAccount = Account


def get_session_repository() -> Iterator[SessionRepository]:
    with open_database(get_database_settings()) as connection:
        validate_database(connection)
        yield SessionRepository(connection)


def require_account(request: Request, repository: Annotated[SessionRepository, Depends(get_session_repository)],
                    boundary: Annotated[None, Depends(require_loopback)]) -> CurrentAccount:
    token = request.cookies.get(COOKIE_NAME, '')
    account = repository.resolve(token) if token else None
    if account is None:
        raise HTTPException(401, 'Authentication required')
    request.state.csrf_token = repository.csrf(token)
    return account


def require_csrf(request: Request, account: Annotated[CurrentAccount, Depends(require_account)]) -> None:
    if not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), request.state.csrf_token):
        raise HTTPException(403, 'Invalid CSRF token')

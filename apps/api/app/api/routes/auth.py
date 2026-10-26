"""Local account registration, sign-in and session lifecycle."""
from typing import Annotated
import os
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from app.api.auth import CurrentAccount, get_session_repository, require_account, require_csrf
from app.api.local_access import require_loopback
from app.schemas.auth import Credentials, LoginCredentials, PasswordChange, SessionResponse
from app.services.access.repository import AccessRepository
from app.services.access.passwords import hash_password, verify_password
from app.services.access.sessions import ABSOLUTE_SECONDS, COOKIE_NAME, COOKIE_PATH, SessionRepository

class AuthRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, 'Invalid authentication input') from None
        return handle


def require_auth_transport(request: Request) -> None:
    if os.environ.get('AUTH_COOKIE_SECURE', 'false').lower() == 'true':
        origin = request.headers.get('origin')
        if request.url.scheme != 'https' or (origin and not origin.startswith('https://')):
            raise HTTPException(403, 'HTTPS is required for secure authentication')


router = APIRouter(prefix='/auth', tags=['auth'], route_class=AuthRoute,
                   dependencies=[Depends(require_loopback), Depends(require_auth_transport)])
Repository = Annotated[SessionRepository, Depends(get_session_repository)]
Authenticated = Annotated[CurrentAccount, Depends(require_account)]
_dummy_hash = hash_password('unusable-login-dummy-password')


def rate_limit(request: Request, repository: Repository) -> None:
    peer = request.client.host
    with repository.connection.transaction():
        repository.connection.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('auth:'+peer,))
        repository.connection.execute("DELETE FROM auth_attempts WHERE attempted_at < now()-interval '15 minutes'")
        count = repository.connection.execute("SELECT count(*) FROM auth_attempts WHERE peer=%s", (peer,)).fetchone()[0]
        if count >= 20:
            raise HTTPException(429, 'Too many authentication attempts', headers={'Retry-After':'900'})
        repository.connection.execute('INSERT INTO auth_attempts(peer) VALUES (%s)', (peer,))


def issue_session(request: Request, response: Response, repository: SessionRepository, account: CurrentAccount) -> dict:
    secure = os.environ.get('AUTH_COOKIE_SECURE', 'false').lower() == 'true'
    old_token = request.cookies.get(COOKIE_NAME)
    if old_token:
        repository.revoke(old_token)
    token, csrf = repository.create(account.id)
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite='strict', secure=secure,
                        path=COOKIE_PATH, max_age=ABSOLUTE_SECONDS)
    response.headers['Cache-Control'] = 'no-store'
    return {'account': account.__dict__, 'csrf_token': csrf}


@router.post('/register', status_code=201, response_model=SessionResponse, dependencies=[Depends(rate_limit)])
def register(body: Credentials, request: Request, response: Response, repository: Repository):
    try:
        account = AccessRepository(repository.connection).create_account(body.email, hash_password(body.password))
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, 'Email is already registered') from None
    return issue_session(request, response, repository, account)


@router.post('/login', response_model=SessionResponse, dependencies=[Depends(rate_limit)])
def login(body: LoginCredentials, request: Request, response: Response, repository: Repository):
    row = repository.connection.execute('SELECT password_hash FROM accounts WHERE email=%s', (body.email,)).fetchone()
    valid = verify_password(body.password, row[0] if row else _dummy_hash)
    account = AccessRepository(repository.connection).account_by_email(body.email)
    if not valid or account is None or account.is_disabled:
        raise HTTPException(401, 'Invalid email or password')
    return issue_session(request, response, repository, account)


@router.get('/session', response_model=SessionResponse)
def session(request: Request, response: Response, account: Authenticated):
    response.headers['Cache-Control'] = 'no-store'
    return {'account':account.__dict__, 'csrf_token':request.state.csrf_token}


@router.post('/logout', status_code=204, dependencies=[Depends(require_csrf)])
def logout(request: Request, repository: Repository):
    repository.revoke(request.cookies[COOKIE_NAME])
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, httponly=True, samesite='strict')
    return response


@router.post('/change-password', status_code=204, dependencies=[Depends(require_csrf)])
def change_password(body: PasswordChange, account: Authenticated, repository: Repository):
    with repository.connection.transaction():
        encoded = repository.connection.execute('SELECT password_hash FROM accounts WHERE id=%s FOR UPDATE', (account.id,)).fetchone()[0]
        if not verify_password(body.current_password, encoded):
            raise HTTPException(401, 'Invalid current password')
        repository.connection.execute('UPDATE accounts SET password_hash=%s WHERE id=%s', (hash_password(body.new_password), account.id))
        repository.revoke_account(account.id)
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return response

"""Opaque sessions: one hour idle timeout and seven day absolute lifetime."""
import hashlib
import secrets
import psycopg
from app.services.access.repository import Account

COOKIE_NAME = 'meeting_session'
COOKIE_PATH = '/api'
ABSOLUTE_SECONDS = 7 * 24 * 60 * 60


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionRepository:
    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def create(self, account_id: str) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.connection.execute('INSERT INTO auth_sessions(token_digest,account_id,csrf_token) VALUES (%s,%s,%s)',
                                (token_digest(token), account_id, csrf))
        return token, csrf

    def resolve(self, token: str) -> Account | None:
        row = self.connection.execute("""UPDATE auth_sessions s SET last_seen_at=now()
            FROM accounts a WHERE s.account_id=a.id AND s.token_digest=%s
            AND NOT a.is_disabled AND s.last_seen_at > now()-interval '1 hour'
            AND s.created_at > now()-interval '7 days'
            RETURNING a.id,a.email,a.is_system_admin,a.is_disabled""", (token_digest(token),)).fetchone()
        if row:
            return Account(*row)
        self.revoke(token)
        return None

    def csrf(self, token: str) -> str:
        return self.connection.execute('SELECT csrf_token FROM auth_sessions WHERE token_digest=%s',
                                       (token_digest(token),)).fetchone()[0]

    def revoke(self, token: str) -> None:
        self.connection.execute('DELETE FROM auth_sessions WHERE token_digest=%s', (token_digest(token),))

    def revoke_account(self, account_id: str) -> None:
        self.connection.execute('DELETE FROM auth_sessions WHERE account_id=%s', (account_id,))

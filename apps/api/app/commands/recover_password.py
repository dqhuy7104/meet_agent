"""Local administrator-operated recovery; no public password reset endpoint."""
import getpass

import psycopg

from app.core.database_config import DatabaseUnavailableError, get_database_settings
from app.db.session import open_database, validate_database
from app.schemas.auth import Credentials
from app.services.access.passwords import hash_password
from app.services.access.sessions import SessionRepository


def recover_password(connection: psycopg.Connection, email: str, password: str) -> None:
    credentials = Credentials(email=email, password=password)
    encoded = hash_password(credentials.password)
    with connection.transaction():
        account = connection.execute(
            'SELECT id FROM accounts WHERE email=%s FOR UPDATE', (credentials.email,)
        ).fetchone()
        if account is None:
            raise ValueError('Account not found')
        connection.execute('UPDATE accounts SET password_hash=%s WHERE id=%s', (encoded, account[0]))
        SessionRepository(connection).revoke_account(account[0])


def main() -> None:
    email = input('Account email (confirm identity externally first): ')
    password = getpass.getpass('New password: ')
    confirmation = getpass.getpass('Confirm new password: ')
    if password != confirmation:
        raise SystemExit('Passwords do not match')
    try:
        with open_database(get_database_settings()) as connection:
            validate_database(connection)
            recover_password(connection, email, password)
    except (DatabaseUnavailableError, psycopg.Error, ValueError):
        raise SystemExit('Recovery refused; check input, account and database configuration') from None
    print('Password updated; all account sessions revoked')


if __name__ == '__main__':
    main()

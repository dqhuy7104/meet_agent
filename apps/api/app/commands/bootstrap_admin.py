"""One-time local administrator bootstrap; passwords never enter command arguments."""
import getpass
import psycopg
from app.core.database_config import DatabaseUnavailableError, get_database_settings
from app.db.session import open_database, validate_database
from app.schemas.auth import Credentials
from app.services.access.passwords import hash_password
from app.services.access.repository import Account, AccessRepository


def bootstrap_admin(connection: psycopg.Connection, email: str, password: str) -> Account:
    credentials = Credentials(email=email, password=password)
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(1296385364, 2)")
        if connection.execute('SELECT EXISTS(SELECT 1 FROM accounts WHERE is_system_admin)').fetchone()[0]:
            raise ValueError('An administrator already exists')
        account = AccessRepository(connection).create_account(credentials.email, hash_password(credentials.password))
        connection.execute('UPDATE accounts SET is_system_admin=true WHERE id=%s', (account.id,))
        return AccessRepository(connection).account_by_id(account.id)


def main() -> None:
    email = input('Admin email: ')
    password = getpass.getpass('Admin password: ')
    try:
        with open_database(get_database_settings()) as connection:
            validate_database(connection)
            bootstrap_admin(connection, email, password)
    except (DatabaseUnavailableError, psycopg.Error, ValueError):
        raise SystemExit('Bootstrap refused; check account state, input and database configuration') from None
    print('Administrator created')


if __name__ == '__main__':
    main()

"""Accounts, projects, and project membership persistence."""

from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Account:
    id: str
    email: str
    is_system_admin: bool
    is_disabled: bool


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    is_archived: bool


@dataclass(frozen=True)
class Membership:
    project_id: str
    account_id: str
    role: str


class AccessRepository:
    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def create_account(self, email: str, password_hash: str) -> Account:
        row = self.connection.execute(
            "INSERT INTO accounts(email,password_hash) VALUES (%s,%s) "
            "RETURNING id,email,is_system_admin,is_disabled",
            (email.strip().lower(), password_hash),
        ).fetchone()
        return Account(*row)

    def account_by_email(self, email: str) -> Account | None:
        row = self.connection.execute(
            "SELECT id,email,is_system_admin,is_disabled FROM accounts WHERE email=%s",
            (email.strip().lower(),),
        ).fetchone()
        return Account(*row) if row else None

    def account_by_id(self, account_id: str) -> Account | None:
        row = self.connection.execute(
            "SELECT id,email,is_system_admin,is_disabled FROM accounts WHERE id=%s",
            (account_id,),
        ).fetchone()
        return Account(*row) if row else None

    def create_project(self, name: str) -> Project:
        row = self.connection.execute(
            "INSERT INTO projects(name) VALUES (%s) RETURNING id,name,is_archived",
            (name.strip(),),
        ).fetchone()
        return Project(*row)

    def project(self, project_id: str) -> Project | None:
        row = self.connection.execute(
            "SELECT id,name,is_archived FROM projects WHERE id=%s", (project_id,)
        ).fetchone()
        return Project(*row) if row else None

    def visible_projects(self, account: Account) -> list[Project]:
        if account.is_disabled:
            return []
        if account.is_system_admin:
            rows = self.connection.execute(
                "SELECT id,name,is_archived FROM projects ORDER BY name,id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT p.id,p.name,p.is_archived FROM projects p "
                "JOIN project_memberships m ON m.project_id=p.id "
                "WHERE m.account_id=%s ORDER BY p.name,p.id", (account.id,)
            ).fetchall()
        return [Project(*row) for row in rows]

    def role(self, account: Account, project_id: str) -> str | None:
        if account.is_disabled:
            return None
        if account.is_system_admin:
            return 'system_admin' if self.project(project_id) else None
        row = self.connection.execute(
            "SELECT role FROM project_memberships WHERE project_id=%s AND account_id=%s",
            (project_id, account.id),
        ).fetchone()
        return row[0] if row else None

    def set_membership(self, project_id: str, account_id: str, role: str) -> Membership:
        if role not in {'project_manager', 'member', 'viewer'}:
            raise ValueError('Invalid project role')
        with self.connection.transaction():
            project = self.connection.execute(
                "SELECT is_archived FROM projects WHERE id=%s FOR UPDATE", (project_id,)
            ).fetchone()
            if project is None:
                raise ValueError('Project not found')
            current = self.connection.execute(
                "SELECT role FROM project_memberships WHERE project_id=%s AND account_id=%s",
                (project_id, account_id),
            ).fetchone()
            if current and current[0] == 'project_manager' and role != 'project_manager':
                self._check_manager_removal(project_id, project[0])
            self.connection.execute(
                "INSERT INTO project_memberships(project_id,account_id,role) VALUES (%s,%s,%s) "
                "ON CONFLICT (project_id,account_id) DO UPDATE SET role=EXCLUDED.role",
                (project_id, account_id, role),
            )
        return Membership(project_id, account_id, role)

    def remove_membership(self, project_id: str, account_id: str) -> None:
        with self.connection.transaction():
            project = self.connection.execute(
                "SELECT is_archived FROM projects WHERE id=%s FOR UPDATE", (project_id,)
            ).fetchone()
            if project is None:
                return
            current = self.connection.execute(
                "SELECT role FROM project_memberships WHERE project_id=%s AND account_id=%s",
                (project_id, account_id),
            ).fetchone()
            if current and current[0] == 'project_manager':
                self._check_manager_removal(project_id, project[0])
            self.connection.execute(
                "DELETE FROM project_memberships WHERE project_id=%s AND account_id=%s",
                (project_id, account_id),
            )

    def _check_manager_removal(self, project_id: str, is_archived: bool) -> None:
        if is_archived:
            return
        count = self.connection.execute(
            "SELECT count(*) FROM project_memberships WHERE project_id=%s AND role='project_manager'",
            (project_id,),
        ).fetchone()[0]
        if count <= 1:
            raise ValueError('Cannot remove the last project manager')

    def accounts(self) -> list[Account]:
        return [Account(*row) for row in self.connection.execute(
            'SELECT id,email,is_system_admin,is_disabled FROM accounts ORDER BY email,id')]

    def members(self, project_id: str) -> list[Membership]:
        return [Membership(*row) for row in self.connection.execute(
            'SELECT project_id,account_id,role FROM project_memberships WHERE project_id=%s ORDER BY account_id',
            (project_id,))]

    def update_project(self, project_id: str, *, name: str | None = None,
                       is_archived: bool | None = None) -> Project:
        row = self.connection.execute('UPDATE projects SET name=COALESCE(%s,name), '
            'is_archived=COALESCE(%s,is_archived) WHERE id=%s RETURNING id,name,is_archived',
            (name.strip() if name else None, is_archived, project_id)).fetchone()
        return Project(*row)

    def update_account(self, account_id: str, *, is_system_admin: bool | None = None,
                       is_disabled: bool | None = None) -> Account | None:
        from app.services.access.sessions import SessionRepository
        with self.connection.transaction():
            # Serialize administrator-count decisions and login's account row lock.
            self.connection.execute("SELECT pg_advisory_xact_lock(hashtext('meeting-account-administration'))")
            row = self.connection.execute('SELECT id,email,is_system_admin,is_disabled FROM accounts '
                                          'WHERE id=%s FOR UPDATE', (account_id,)).fetchone()
            if row is None:
                return None
            current = Account(*row)
            if current.is_system_admin and not current.is_disabled and (is_system_admin is False or is_disabled is True):
                count = self.connection.execute('SELECT count(*) FROM accounts WHERE is_system_admin AND NOT is_disabled').fetchone()[0]
                if count <= 1:
                    raise ValueError('Cannot remove the last system administrator')
            row = self.connection.execute('UPDATE accounts SET is_system_admin=COALESCE(%s,is_system_admin), '
                'is_disabled=COALESCE(%s,is_disabled) WHERE id=%s RETURNING id,email,is_system_admin,is_disabled',
                (is_system_admin, is_disabled, account_id)).fetchone()
            if is_disabled:
                SessionRepository(self.connection).revoke_account(account_id)
            return Account(*row)

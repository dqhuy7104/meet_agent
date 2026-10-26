import httpx
from apps.api.tests.postgres_support import PostgresAsyncTestCase
from app.services.access.repository import AccessRepository
from app.services.access.sessions import SessionRepository, COOKIE_NAME


class ProjectRoutesTest(PostgresAsyncTestCase):
    async def asyncSetUp(self):
        from app.main import app
        self.repository = AccessRepository(self.connection)
        self.admin = self.repository.create_account('admin@example.com', 'hash')
        self.connection.execute('UPDATE accounts SET is_system_admin=true WHERE id=%s', (self.admin.id,))
        self.manager = self.repository.create_account('manager@example.com', 'hash')
        self.member = self.repository.create_account('member@example.com', 'hash')
        self.outsider = self.repository.create_account('outside@example.com', 'hash')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1',1234)), base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)
        self.as_account(self.admin)

    def as_account(self, account):
        token, csrf = SessionRepository(self.connection).create(account.id)
        self.client.cookies.set(COOKIE_NAME, token)
        self.client.headers['X-CSRF-Token'] = csrf

    async def project(self):
        response = await self.client.post('/api/projects', json={'name':'Research'})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    async def test_admin_archive_restore_and_last_admin(self):
        project = await self.project()
        self.assertEqual((await self.client.post(f'/api/projects/{project}/archive')).status_code,200)
        self.assertTrue((await self.client.get(f'/api/projects/{project}')).json()['is_archived'])
        self.assertEqual((await self.client.post(f'/api/projects/{project}/restore')).status_code,200)
        for payload in ({'is_disabled':True}, {'is_system_admin':False}):
            self.assertEqual((await self.client.patch(f'/api/admin/accounts/{self.admin.id}',json=payload)).status_code,409)

    async def test_membership_boundaries_and_final_manager(self):
        project = await self.project()
        response = await self.client.put(f'/api/admin/projects/{project}/managers/{self.manager.id}')
        self.assertEqual(response.status_code,200,response.text)
        self.as_account(self.manager)
        url=f'/api/projects/{project}/members/{self.member.id}'
        self.assertEqual((await self.client.put(url,json={'role':'member'})).status_code,200)
        self.assertEqual((await self.client.get(f'/api/projects/{project}/members')).status_code,200)
        self.assertEqual((await self.client.get(f'/api/projects/{project}/accounts/lookup',params={'email':self.member.email})).status_code,200)
        self.assertEqual((await self.client.put(url,json={'role':'project_manager'})).status_code,403)
        self.assertEqual((await self.client.delete(f'/api/projects/{project}/members/{self.manager.id}')).status_code,403)
        self.as_account(self.member)
        self.assertEqual((await self.client.put(url,json={'role':'viewer'})).status_code,403)
        self.as_account(self.outsider)
        self.assertEqual((await self.client.get(f'/api/projects/{project}')).status_code,404)
        self.assertEqual((await self.client.get('/api/projects')).json(),[])
        self.as_account(self.admin)
        self.assertEqual((await self.client.delete(f'/api/projects/{project}/members/{self.manager.id}')).status_code,409)

    async def test_disabling_revokes_sessions_after_enable(self):
        sessions=SessionRepository(self.connection)
        token,_=sessions.create(self.member.id)
        response=await self.client.patch(f'/api/admin/accounts/{self.member.id}',json={'is_disabled':True})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIsNone(sessions.resolve(token))
        await self.client.patch(f'/api/admin/accounts/{self.member.id}',json={'is_disabled':False})
        self.assertIsNone(sessions.resolve(token))

    async def test_policy_archive_and_meeting_ownership(self):
        from app.services.access.policy import require_project, require_meeting
        from fastapi import HTTPException
        project=await self.project()
        self.repository.set_membership(project,self.member.id,'member')
        self.repository.set_membership(project,self.outsider.id,'viewer')
        self.connection.execute("INSERT INTO meetings(id,original_filename,storage_key,checksum,status,created_at,updated_at,project_id,uploaded_by_account_id) VALUES ('m','a','k','c','queued',now(),now(),%s,%s)",(project,self.member.id))
        self.assertEqual(require_meeting(self.member,'m','retry',self.repository).id,'m')
        with self.assertRaises(HTTPException) as raised:
            require_meeting(self.outsider,'m','delete',self.repository)
        self.assertEqual(raised.exception.status_code,403)
        await self.client.post(f'/api/projects/{project}/archive')
        self.assertTrue(require_project(self.member,project,'read',self.repository).is_archived)
        with self.assertRaises(HTTPException) as raised:
            require_project(self.member,project,'upload',self.repository)
        self.assertEqual(raised.exception.status_code,403)

    async def test_csrf_unknown_roles_and_admin_routes_hidden_from_manager(self):
        project = await self.project()
        self.repository.set_membership(project,self.manager.id,'project_manager')
        self.as_account(self.manager)
        self.assertEqual((await self.client.get('/api/admin/accounts')).status_code,403)
        self.assertEqual((await self.client.patch(f'/api/projects/{project}',json={'name':'Renamed'})).status_code,403)
        self.assertEqual((await self.client.put(f'/api/projects/{project}/members/{self.member.id}',json={'role':'admin'})).status_code,422)
        self.client.headers.pop('X-CSRF-Token')
        self.assertEqual((await self.client.put(f'/api/projects/{project}/members/{self.member.id}',json={'role':'member'})).status_code,403)

    async def test_viewer_ownership_is_insufficient_and_archived_membership_manageable(self):
        from fastapi import HTTPException
        from app.services.access.policy import require_meeting, require_project
        project = await self.project()
        self.repository.set_membership(project,self.manager.id,'project_manager')
        self.repository.set_membership(project,self.member.id,'member')
        self.repository.set_membership(project,self.outsider.id,'viewer')
        self.connection.execute("INSERT INTO meetings(id,original_filename,storage_key,checksum,status,created_at,updated_at,source,project_id,uploaded_by_account_id) VALUES ('bot','a','k','c','queued',now(),now(),'bot',%s,%s)",(project,self.member.id))
        for action in ('delete','retry','identify'):
            with self.assertRaises(HTTPException) as raised:
                require_meeting(self.member,'bot',action,self.repository)
            self.assertEqual(raised.exception.status_code,403)
        with self.assertRaises(HTTPException):
            require_project(self.outsider,project,'upload',self.repository)
        await self.client.post(f'/api/projects/{project}/archive')
        self.as_account(self.manager)
        response=await self.client.put(f'/api/projects/{project}/members/{self.member.id}',json={'role':'viewer'})
        self.assertEqual(response.status_code,200,response.text)

    async def test_disable_waits_for_login_account_lock_and_revokes_new_session(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        import psycopg
        with psycopg.connect(self.database.database_url.get_secret_value(), autocommit=True) as login_connection, psycopg.connect(self.database.database_url.get_secret_value(), autocommit=True) as admin_connection:
            with ThreadPoolExecutor(max_workers=1) as executor:
                with login_connection.transaction():
                    login_connection.execute('SELECT id FROM accounts WHERE id=%s FOR UPDATE',(self.member.id,))
                    change=executor.submit(AccessRepository(admin_connection).update_account,self.member.id,is_disabled=True)
                    deadline=time.monotonic()+5
                    while not change.done():
                        row=self.connection.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',(admin_connection.info.backend_pid,)).fetchone()
                        if row and row[0]=='Lock':
                            break
                        self.assertLess(time.monotonic(),deadline)
                        time.sleep(0.01)
                    self.assertFalse(change.done(),'Disable must wait for login to finish')
                    token,_=SessionRepository(login_connection).create(self.member.id)
                self.assertTrue(change.result(timeout=5).is_disabled)
        self.assertIsNone(SessionRepository(self.connection).resolve(token))

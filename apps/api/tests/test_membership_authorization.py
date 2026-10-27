"""Grant protection uses stored roles even when a target account is disabled."""
from unittest import TestCase
from unittest.mock import Mock, patch

from fastapi import HTTPException
from app.api.routes.projects import authorize_membership_change
from app.services.access.repository import Account, Membership


class MembershipAuthorizationTest(TestCase):
    def test_disabled_manager_membership_still_requires_admin(self):
        manager = Account('actor', 'actor@example.test', False, False)
        target = Account('target', 'target@example.test', False, True)
        repository = Mock()
        repository.account_by_id.return_value = target
        repository.members.return_value = [Membership('project', target.id, 'project_manager')]
        with patch('app.api.routes.projects.require_project'):
            for role in ('member', 'viewer', None):
                with self.subTest(role=role), self.assertRaises(HTTPException) as error:
                    authorize_membership_change(manager, 'project', target.id, role, repository)
                self.assertEqual(error.exception.status_code, 403)
            admin = Account('admin', 'admin@example.test', True, False)
            authorize_membership_change(admin, 'project', target.id, 'viewer', repository)

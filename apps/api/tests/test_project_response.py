"""Project responses expose the caller's server-derived role."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from app.api.routes.projects import project_response

class ProjectResponseTests(unittest.TestCase):
    def test_role_comes_from_repository_for_each_caller(self):
        project = SimpleNamespace(id='project', name='Research', is_archived=False)
        account = SimpleNamespace(id='account')
        for role in ('system_admin', 'project_manager', 'member', 'viewer'):
            with self.subTest(role=role):
                repository = Mock()
                repository.role.return_value = role
                response = project_response(project, account, repository)
                self.assertEqual(response.role, role)
                repository.role.assert_called_once_with(account, project.id)

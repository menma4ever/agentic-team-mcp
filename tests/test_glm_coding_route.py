import os
import unittest
from unittest.mock import patch
from core.config import SystemSettings
from engine.models import AgentNode, Role, HarnessType
from harness.cli_runner import CLIRunner


class GLMCodingRouteTests(unittest.TestCase):
    def test_scoped_coding_route_no_api_balance_fallback(self):
        config = SystemSettings(api_keys={'zai': 'test-zai-not-secret'})
        runner = CLIRunner(config)
        agent = AgentNode(project_name='test', name='worker', role=Role.WORKER,
                          harness=HarnessType.CLAUDE_CODE, model='zai/glm-5.3-flash')
        with patch.object(runner, 'resolve', return_value=['claude.exe']), patch.dict(os.environ, {
                'ANTHROPIC_API_KEY': 'unrelated', 'ANTHROPIC_BASE_URL': 'https://wrong.invalid',
                'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'wrong', 'TEAM_OWNER_TOKEN': 'owner'}):
            args, env, _ = runner.build(agent, 'task', 'C:/scoped-worker', 'http://127.0.0.1:8765', 'scoped')
        self.assertEqual(env['ANTHROPIC_BASE_URL'], 'https://api.z.ai/api/anthropic')
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], 'test-zai-not-secret')
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotIn('TEAM_OWNER_TOKEN', env)
        self.assertEqual(env['TEAM_TOKEN'], 'scoped')
        self.assertEqual(env['ANTHROPIC_DEFAULT_HAIKU_MODEL'], 'glm-5.3-flash')
        self.assertNotIn('test-zai-not-secret', ' '.join(args))

    def test_missing_key_fails_closed(self):
        runner = CLIRunner(SystemSettings())
        with patch.object(runner, 'resolve', return_value=['claude.exe']), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'Coding Plan key'):
                runner.validate(HarnessType.CLAUDE_CODE, 'zai/glm-5.3-flash')

    def test_unknown_model_fails_closed(self):
        runner = CLIRunner(SystemSettings(api_keys={'zai':'test'}))
        with patch.object(runner, 'resolve', return_value=['claude.exe']):
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                runner.validate(HarnessType.CLAUDE_CODE, 'zai/missing')

if __name__ == '__main__':
    unittest.main()

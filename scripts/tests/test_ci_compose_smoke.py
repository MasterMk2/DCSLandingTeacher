"""The destructive cleanup helper must reject operator environments before Docker is called."""

import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("compose_smoke", Path(__file__).parents[1] / "ci_compose_smoke.py")
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class ComposeSmokeSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                    "COMPOSE_PROJECT_NAME": "dlt-ci-123-2", "DLT_HOST": "127.0.0.1", "DLT_PORT": "18080"}

    def test_accepts_only_unique_loopback_ci_project(self):
        self.assertEqual(smoke.validate_environment(self.root, self.env), "dlt-ci-123-2")

    def test_rejects_non_ci_and_wrong_project_or_listener(self):
        for key, value in [("GITHUB_ACTIONS", "false"), ("GITHUB_RUN_ID", "local"),
                           ("COMPOSE_PROJECT_NAME", "production"), ("DLT_HOST", "0.0.0.0"),
                           ("DLT_PORT", "8080"), ("DOCKER_HOST", "tcp://example.invalid:2375"),
                           ("DOCKER_CONTEXT", "production")]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                smoke.validate_environment(self.root, {**self.env, key: value})

    def test_refuses_existing_environment_file(self):
        (self.root / ".env").write_text("existing")
        with self.assertRaises(RuntimeError):
            smoke.validate_environment(self.root, self.env)
        self.assertEqual((self.root / ".env").read_text(), "existing")

    def test_short_websocket_read_is_rejected(self):
        with self.assertRaises(AssertionError):
            smoke.read_exact(io.BytesIO(b"a"), 2)

    def test_full_example_keeps_relative_cache_override_for_regression(self):
        example = (Path(__file__).parents[2] / '.env.example').read_text()
        result = smoke.example_environment_for_ci(example)
        self.assertIn('DLT_RUNWAY_CACHE_DIR=cache\n', result)
        self.assertIn('DLT_ACMI_ENABLED=false\n', result)
        self.assertIn('DLT_DCSSB_BASE_URL=\n', result)
        original_keys = {line.split('=', 1)[0] for line in example.splitlines()
                         if line and not line.startswith('#')}
        result_keys = {line.split('=', 1)[0] for line in result.splitlines()
                       if line and not line.startswith('#')}
        self.assertEqual(original_keys, result_keys)

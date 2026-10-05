"""Development checks must stop on failure, require migration DB and avoid private diagnostics."""
import contextlib
import importlib.util
import io
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('dev_entry', Path(__file__).parents[2] / 'tools/dev.py')
assert SPEC and SPEC.loader
dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev)


class DevelopmentEntry(unittest.TestCase):
    def test_check_propagates_failure_and_stops_before_tests(self):
        with patch.object(dev, 'doctor', return_value=0), patch.object(
            dev.subprocess, 'run', return_value=subprocess.CompletedProcess([], 7)
        ) as run:
            self.assertEqual(dev.check('backend'), 7)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][1:4], ['-m', 'ruff', 'check'])
        self.assertIn('../tools/dev.py', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['cwd'], dev.ROOT / 'backend')

    def test_wrong_environment_does_not_start_checks(self):
        with patch.object(dev, 'doctor', return_value=1), patch.object(dev.subprocess, 'run') as run:
            self.assertEqual(dev.check('backend'), 1)
        run.assert_not_called()

    def test_migration_check_requires_explicit_test_database(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(dev, 'doctor', return_value=0), \
                patch.object(dev.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dev.check('migration-job'), 1)
        run.assert_not_called()

    def test_doctor_reports_versions_only_and_rejects_uv_mismatch(self):
        output = io.StringIO()
        private_value = 'private-example.invalid/password'
        with patch.dict(os.environ, {'DLT_TEST_POSTGRES_URL': private_value}), patch.object(
            dev.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'uv 0.12.5 extra')
        ), patch.object(dev.importlib.metadata, 'version', return_value='test-version'), \
                patch.object(dev.platform, 'python_version', return_value='3.11.15'), \
                contextlib.redirect_stdout(output):
            self.assertEqual(dev.doctor('backend'), 1)
        self.assertNotIn(private_value, output.getvalue())
        self.assertNotIn(str(dev.ROOT), output.getvalue())
        self.assertIn('"uv": "0.12.5"', output.getvalue())


if __name__ == '__main__':
    unittest.main()

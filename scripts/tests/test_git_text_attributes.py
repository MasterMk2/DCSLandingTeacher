"""Verify UTF-8 BOM preservation with actual Git staging and checkout on both OSes."""

import codecs
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class GitTextAttributes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'core.autocrlf', 'false')

    def git(self, *args, check=True):
        return subprocess.run(['git', '-C', str(self.repo), *args],
                              capture_output=True, check=check)

    def test_bom_and_crlf_survive_staging_and_checkout(self):
        (self.repo / '.gitattributes').write_bytes((ROOT / '.gitattributes').read_bytes())
        self.git('add', '.gitattributes')
        for suffix in ('ps1', 'iss'):
            with self.subTest(suffix=suffix):
                name = 'example.' + suffix
                original = codecs.BOM_UTF8 + "# 日本語の保持\r\n# second line\r\n".encode()
                (self.repo / name).write_bytes(original)
                self.git('add', '--', name)
                staged = self.git('show', ':' + name).stdout
                self.assertEqual(staged, original.replace(b'\r\n', b'\n'))
                (self.repo / name).unlink()
                self.git('checkout-index', '-f', '--', name)
                self.assertEqual((self.repo / name).read_bytes(), original)

    def test_record_legacy_encoding_support_without_assuming_platform_result(self):
        (self.repo / '.gitattributes').write_text('*.ps1 text eol=crlf working-tree-encoding=UTF-8-BOM\n')
        (self.repo / 'legacy.ps1').write_bytes(codecs.BOM_UTF8 + b'# fixture\r\n')
        result = self.git('add', 'legacy.ps1', check=False)
        print(f'Legacy UTF-8-BOM encoding supported on {os.name}: {result.returncode == 0}')
        if result.returncode:
            self.assertIn(b'failed to encode', result.stderr)

    def test_tracked_windows_native_files_keep_required_bom(self):
        result = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z', '--', '*.ps1', '*.iss'],
                                capture_output=True, check=True)
        for name in result.stdout.decode('utf-8').split('\0'):
            if name:
                with self.subTest(path=name):
                    self.assertTrue((ROOT / name).read_bytes().startswith(codecs.BOM_UTF8))

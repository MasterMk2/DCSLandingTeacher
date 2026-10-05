"""Small doctor/check entry points; restoration and deployment stay explicit."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def doctor(project: str) -> int:
    expected_python = (ROOT / '.python-version').read_text(encoding='utf-8').strip()
    manifest = tomllib.loads((ROOT / project / 'pyproject.toml').read_text(encoding='utf-8'))
    expected_uv = manifest['tool']['uv']['required-version'].removeprefix('==')
    try:
        result = subprocess.run(['uv', '--version'], capture_output=True, text=True, check=False)
        match = re.match(r'uv ([0-9.]+)\b', result.stdout) if result.returncode == 0 else None
        uv_version = match[1] if match else 'unavailable'
    except OSError:
        uv_version = 'unavailable'
    packages = ['ruff', 'pytest'] + (['basedpyright', 'migration-job'] if project == 'migration-job' else [])
    installed = {}
    for package in packages:
        try:
            installed[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            installed[package] = 'unavailable'
    print(json.dumps({
        'project': project, 'python': platform.python_version(), 'uv': uv_version,
        'platform': platform.system(), 'arch': platform.machine(),
        'expectedPython': expected_python, 'expectedUv': expected_uv, 'tools': installed,
    }, indent=2), flush=True)
    valid = platform.python_version() == expected_python and uv_version == expected_uv
    return 0 if valid and 'unavailable' not in installed.values() else 1


def check(project: str) -> int:
    if doctor(project):
        return 1
    if project == 'migration-job' and not os.environ.get('DLT_TEST_POSTGRES_URL'):
        print('Set DLT_TEST_POSTGRES_URL to a disposable test database; migration check requires it.')
        return 1
    commands = [[sys.executable, '-m', 'ruff', 'check', '.']]
    if project == 'backend':
        commands[0] = [sys.executable, '-m', 'ruff', 'check', '--config', 'pyproject.toml',
                       '.', '../tools/dev.py', '../scripts/tests/test_dev_entry.py']
    if project == 'migration-job':
        commands.append([sys.executable, '-m', 'basedpyright'])
    commands.append([sys.executable, '-m', 'pytest', '-q'])
    if project == 'migration-job':
        commands.append([sys.executable, '-m', 'pytest', '../scripts/tests/test_migrate_sqlite.py', '-q'])
    for command in commands:
        result = subprocess.run(command, cwd=ROOT / project, check=False)
        if result.returncode:
            return result.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['doctor', 'check'])
    parser.add_argument('--project', choices=['backend', 'migration-job'], default=Path.cwd().name)
    args = parser.parse_args()
    if args.project not in ('backend', 'migration-job'):
        parser.error('Run from backend/migration-job or pass --project explicitly.')
    return doctor(args.project) if args.command == 'doctor' else check(args.project)


if __name__ == '__main__':
    raise SystemExit(main())

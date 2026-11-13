#!/usr/bin/env python3
"""Run selected component tests; use --all for an explicit full check."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('components', nargs='*', help='component directories, e.g. gateway worker')
    parser.add_argument('--all', action='store_true', help='check all components')
    parser.add_argument('--postgres', action='store_true',
                        help='include tests requiring the designated disposable database')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    available = {p.name for p in root.iterdir()
                 if p.is_dir() and any(p.glob('test_*.py'))}
    if not args.all and not args.components:
        parser.error('select components or use --all')
    if set(args.components) - available:
        parser.error('unknown components: ' + ', '.join(sorted(set(args.components) - available)))
    environment = dict(os.environ, PYTHONPATH=str(root))
    failed = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not any(directory.glob('test_*.py')):
            continue
        if not args.all and directory.name not in args.components:
            continue
        if directory.name == 'database' and not args.postgres:
            print('database: skipped (use --postgres with its disposable test database)', flush=True)
            continue
        print(f'Checking {directory.name}', flush=True)
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover',
                                 '-s', str(directory), '-p', 'test_*.py'],
                                cwd=root, env=environment)
        if result.returncode:
            failed.append(directory.name)
    if failed:
        print('Failed components: ' + ', '.join(failed), file=sys.stderr)
    return bool(failed)


if __name__ == '__main__':
    sys.exit(main())

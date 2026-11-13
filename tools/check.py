#!/usr/bin/env python3
"""Run each component suite in isolation; database integration is explicit opt-in."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', action='store_true',
                        help='include tests requiring the designated disposable database')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    environment = dict(os.environ, PYTHONPATH=str(root))
    failed = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not any(directory.glob('test_*.py')):
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

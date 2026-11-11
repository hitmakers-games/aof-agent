"""Fail closed unless the host can enforce Codex filesystem permissions."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def check():
    Path(os.environ["CODEX_HOME"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".sandbox-check-", dir="/workspace") as directory:
        # This public canary is baked into the image under a managed deny_read
        # path. No real login or repository is needed for this startup check.
        script = """
import errno
from pathlib import Path
for name in ['/run/secrets/aof-sandbox-canary', '/var/run/secrets/aof-sandbox-canary']:
    try:
        Path(name).read_text()
    except PermissionError:
        pass
    else:
        raise SystemExit('Sandbox did not deny credential-path access')
target = Path('write-check')
try:
    target.write_text('synthetic')
except OSError as error:
    if MODE == 'workspace-write' or error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
        raise
else:
    if MODE == 'read-only':
        raise SystemExit('Read-only sandbox allowed a workspace write')
"""
        for mode, profile in [("read-only", ":read-only"), ("workspace-write", ":workspace")]:
            subprocess.run([
                "codex", "sandbox", "--include-managed-config", "-P", profile,
                "-C", directory, "--", "python3", "-c", f"MODE = {mode!r}\n" + script,
            ], check=True, timeout=30, stdin=subprocess.DEVNULL)


if __name__ == "__main__":
    try:
        check()
    except (OSError, subprocess.SubprocessError):
        print("Codex sandbox preflight failed; worker startup refused", file=sys.stderr)
        sys.exit(1)
    print("Codex sandbox preflight passed")

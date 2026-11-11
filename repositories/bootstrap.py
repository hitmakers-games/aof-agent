"""Populate the dedicated workspace without resetting or cleaning existing work."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from github_app import AccessError, GitHubApp


WORKSPACE = Path("/workspace")
HELPER = Path(__file__).with_name("credential.py").resolve()


def git(arguments, directory=None):
    command = ["git", "-c", "credential.helper=", "-c", f"credential.helper=!python3 {HELPER}",
               "-c", "credential.useHttpPath=true", "-c", "core.hooksPath=/dev/null",
               "-c", "core.fsmonitor=false", "-c", "http.followRedirects=false",
               "-c", "pack.threads=1", "-c", "index.threads=1", *arguments]
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
                       GIT_CONFIG_GLOBAL="/dev/null", GIT_ALLOW_PROTOCOL="https", GIT_LFS_SKIP_SMUDGE="1")
    return subprocess.run(command, cwd=directory, env=environment, capture_output=True,
                          text=True, timeout=1200)


def validate(metadata):
    name = metadata["name"]
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", name)
            or metadata["owner"]["login"] != "hitmakers-games"
            or metadata["clone_url"] != f"https://github.com/hitmakers-games/{name}.git"):
        raise AccessError("Repository metadata does not match the corporate allowlist.")
    return name


def populate(root, metadata):
    name = validate(metadata)
    destination = root / name
    if destination.is_symlink():
        raise AccessError(f"Refusing a symlink workspace for {name}.")
    if destination.exists():
        origin = git(["remote", "get-url", "origin"], destination)
        if origin.returncode or origin.stdout.strip() != metadata["clone_url"]:
            raise AccessError(f"Existing {name} does not match its expected Git remote; left untouched.")
        state = "existing; left untouched"
    else:
        stage = Path(tempfile.mkdtemp(prefix=f".clone-{name}-", dir=root))
        result = git(["-c", "init.defaultBranch=" + metadata["default_branch"], "clone",
                      "--no-recurse-submodules", "--", metadata["clone_url"], str(stage)])
        if result.returncode:
            raise AccessError(f"Clone failed for {name}; partial clone preserved at {stage.name}.")
        stage.rename(destination)
        state = "cloned"
    head = git(["rev-parse", "--verify", "HEAD"], destination)
    branches = git(["branch", "--remotes"], destination)
    return {"name": name, "path": str(destination), "remote": metadata["clone_url"],
            "default_branch": metadata["default_branch"], "head": head.stdout.strip() if not head.returncode else None,
            "empty": not branches.stdout.strip(), "state": state}


def main():
    os.umask(0o077)
    if not os.path.ismount(WORKSPACE) or WORKSPACE.is_symlink():
        raise AccessError("/workspace must be the dedicated persistent-volume mount.")
    if shutil.disk_usage(WORKSPACE).free < 512 * 1024 * 1024:
        raise AccessError("Workspace has less than 512 MiB free; expand it before cloning.")
    root = WORKSPACE / "repos"
    if root.is_symlink():
        raise AccessError("The repository root cannot be a symlink.")
    root.mkdir(mode=0o700, exist_ok=True)
    (WORKSPACE / "tasks").mkdir(mode=0o700, exist_ok=True)
    with (WORKSPACE / ".repositories.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app = GitHubApp()
        app.verify_installation()
        repositories = sorted(app.repositories(), key=lambda item: item["name"])
        if not repositories:
            raise AccessError("The App has no accessible repositories.")
        for metadata in repositories:
            validate(metadata)
        print(f"Verified App installation; {len(repositories)} corporate repositories accessible.", flush=True)
        inventory = []
        for metadata in repositories:
            item = populate(root, metadata)
            inventory.append(item)
            temporary = WORKSPACE / ".repositories.json.tmp"
            temporary.write_text(json.dumps({"organization": app.organization, "repositories": inventory}, indent=2) + "\n")
            temporary.replace(WORKSPACE / "repositories.json")
            print(item["name"] + ": " + item["state"] + (" (empty remote)" if item["empty"] else ""), flush=True)
        print("Workspace bootstrap complete. No builds, hooks, submodules, or remote writes were run.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except AccessError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, subprocess.SubprocessError):
        # Never log API responses, Git stderr, signing output, or credential material.
        print("Workspace bootstrap stopped; inspect storage, App configuration, and repository availability.", file=sys.stderr)
        sys.exit(1)

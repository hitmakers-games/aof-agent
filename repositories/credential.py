"""Git credential helper: mint a read-only token for one corporate repository."""
import re
import sys

from github_app import AccessError, GitHubApp


def repository(fields):
    if fields.get("protocol") != "https" or fields.get("host") != "github.com":
        raise AccessError("Credentials are only available for GitHub HTTPS.")
    match = re.fullmatch(r"hitmakers-games/([A-Za-z0-9][A-Za-z0-9._-]{0,99})\.git", fields.get("path", ""))
    if not match:
        raise AccessError("Credentials are only available for corporate repositories.")
    return match[1]


def main():
    if len(sys.argv) != 2 or sys.argv[1] != "get":
        return 0  # Never store credentials supplied by Git.
    fields = dict(line.split("=", 1) for line in sys.stdin.read(8192).splitlines() if "=" in line)
    name = repository(fields)
    token = GitHubApp().token(name)
    # This stdout is a private pipe to Git, never an application log.
    print("username=x-access-token\npassword=" + token + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AccessError, OSError, ValueError):
        print("GitHub credential lookup failed; no credentials stored.", file=sys.stderr)
        sys.exit(1)

"""GitHub App authentication using a mounted key and in-memory tokens."""
import base64
import json
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class AccessError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


class GitHubApp:
    def __init__(self, secret_dir="/run/secrets/github", organization="hitmakers-games"):
        directory = Path(secret_dir)
        self.key = directory / "private-key.pem"
        self.app_id = int((directory / "app-id").read_text().strip())
        self.installation_id = int((directory / "installation-id").read_text().strip())
        self.organization = organization
        self.opener = build_opener(NoRedirects())

    def jwt(self):
        now = int(time.time())
        header = encoded(b'{"alg":"RS256","typ":"JWT"}')
        claims = encoded(json.dumps({"iat": now - 60, "exp": now + 540,
                                     "iss": str(self.app_id)}).encode())
        unsigned = header + b"." + claims
        signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(self.key)],
                                input=unsigned, capture_output=True, timeout=10)
        if signed.returncode:
            raise AccessError("GitHub App signing failed; check the mounted key.")
        return (unsigned + b"." + encoded(signed.stdout)).decode()

    def request(self, path, token, body=None, method=None):
        if not path.startswith("/") or ".." in path:
            raise AccessError("Invalid GitHub API path")
        headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "aof-agent-repositories"}
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request("https://api.github.com" + path, headers=headers, data=data, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                result = response.read(2 * 1024 * 1024 + 1)
            if len(result) > 2 * 1024 * 1024:
                raise AccessError("GitHub response exceeded the metadata limit.")
            return json.loads(result)
        except HTTPError as error:
            raise AccessError(f"GitHub API rejected access (HTTP {error.code}); credentials omitted.") from None
        except (OSError, URLError, ValueError):
            raise AccessError("GitHub metadata request failed; credentials omitted.") from None

    def verify_installation(self):
        data = self.request(f"/app/installations/{self.installation_id}", self.jwt())
        if (data["app_id"] != self.app_id or data["account"]["login"] != self.organization
                or data.get("suspended_at")):
            raise AccessError("The App installation does not match the configured organization.")

    def token(self, repository=None, permissions=None):
        body = {"permissions": permissions or {"contents": "read"}}
        if repository is not None:
            body["repositories"] = [repository]
        return self.request(f"/app/installations/{self.installation_id}/access_tokens",
                            self.jwt(), body)["token"]

    def repositories(self):
        token = self.token()
        result = []
        for page in range(1, 101):
            batch = self.request(f"/installation/repositories?per_page=100&page={page}", token)["repositories"]
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise AccessError("Repository listing exceeded the pagination limit.")

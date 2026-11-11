"""Small read-only Kubernetes API client with fixed stand and component mappings."""
import json
from pathlib import Path
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import ProxyHandler, Request, build_opener, HTTPSHandler

from gateway.commands import STANDS, COMPONENTS


class DiagnosticError(Exception):
    pass


class Kubernetes:
    def __init__(self, credential_dir="/var/run/agent-kube"):
        self.credentials = Path(credential_dir)
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(
            context=ssl.create_default_context(cafile=str(self.credentials / "ca.crt"))))

    def get(self, path, query=None, raw=False):
        url = "https://kubernetes.default.svc" + path
        if query:
            url += "?" + urlencode(query)
        # Projected tokens rotate; read the current token for every request.
        request = Request(url, headers={"Authorization": "Bearer " + (self.credentials / "token").read_text().strip()})
        try:
            with self.opener.open(request, timeout=5) as response:
                data = response.read(256 * 1024 + 1)
        except HTTPError as error:
            raise DiagnosticError(f"Kubernetes read failed (HTTP {error.code}). Check observer permissions and resource availability.") from None
        except (OSError, URLError):
            raise DiagnosticError("Kubernetes API is unavailable; no changes were made.") from None
        if len(data) > 256 * 1024:
            raise DiagnosticError("Diagnostic response exceeded the 256 KiB limit. Use a narrower request.")
        try:
            return data.decode("utf-8", "replace") if raw else json.loads(data)
        except ValueError:
            raise DiagnosticError("Kubernetes returned an invalid response; raw data omitted.") from None

    def deployment(self, stand, component):
        namespace, name = STANDS[stand], COMPONENTS[component]
        return self.get(f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}")

    def pods(self, stand, deployment):
        labels = deployment["spec"]["selector"]["matchLabels"]
        selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return self.get(f"/api/v1/namespaces/{STANDS[stand]}/pods", {"labelSelector": selector})["items"]

    def logs(self, stand, pod, container, minutes):
        return self.get(f"/api/v1/namespaces/{STANDS[stand]}/pods/{quote(pod, safe='')}/log",
                        {"container": container, "tailLines": 200, "limitBytes": 65536,
                         "sinceSeconds": minutes * 60, "timestamps": "true"}, raw=True)

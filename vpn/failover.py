"""Unprivileged watchdog: reconnect first, then change Amnezia location."""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from connection_key import decode_key
from subscription import b64, config_payload, native_configuration, request

HEARTBEAT = Path("/watchdog/heartbeat")


class Conflict(Exception):
    pass


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def decode_field(secret, name):
    return base64.b64decode(secret["data"][name]).decode()


def control_state(secret):
    value = secret.get("data", {}).get("failover.json")
    return json.loads(base64.b64decode(value)) if value else {}


def next_country(countries, current):
    # Prefer an explicit country before the provider's automatic/global option.
    choices = sorted({c["server_country_code"] for c in countries
                      if "awg" in c.get("available_protocols", [])
                      and isinstance(c.get("server_country_code"), str)}, key=lambda c: (c == "gl", c))
    if current in choices:
        position = choices.index(current)
        choices = choices[position + 1:] + choices[:position]
    return next((c for c in choices if c != current), None)


def eligible(health, state, now, reconnects=2):
    control = control_state(state)
    if control.get("phase") == "blocked" or now < control.get("next_action_at", 0):
        return False
    if control.get("phase") == "provisioning":
        return True
    # Never rotate countries for a dead pod, stale status, or a configuration
    # that has not yet reached the gateway's projected Secret volume.
    return (
        isinstance(health, dict) and 0 <= now - health.get("heartbeat", 0) < 90
        and health.get("config_id") == digest(decode_field(state, "primary.conf"))
        and health.get("generation") == control.get("generation")
        and not health.get("ready", False)
        and health.get("failed_attempts", 0) >= reconnects
    )


class Kubernetes:
    def __init__(self):
        self.namespace = os.environ["POD_NAMESPACE"]
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        self.base = f"https://{host}:{port}/api/v1/namespaces/{self.namespace}/secrets/"
        self.token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        self.tls = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

    def call(self, name, data=None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + name, data=body,
                                     method="PATCH" if body is not None else "GET",
                                     headers={"Authorization": "Bearer " + self.token.read_text().strip(),
                                              "Content-Type": "application/merge-patch+json"})
        try:
            with urllib.request.urlopen(req, context=self.tls, timeout=10) as response:
                raw = response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as error:
            if error.code == 409:
                raise Conflict() from None
            raise RuntimeError("Kubernetes API request failed") from None
        return json.loads(raw)

    def get(self, name):
        return self.call(name)

    def patch(self, name, data, version):
        return self.call(name, {"metadata": {"resourceVersion": version}, "data": data})


class Watchdog:
    def __init__(self, api, cooldown=300, reconnects=2, emit=None):
        self.api = api
        self.cooldown = cooldown
        self.reconnects = reconnects
        self.source_name = os.environ.get("SOURCE_SECRET", "aof-agent-vpn-config")
        self.state_name = os.environ.get("STATE_SECRET", "aof-agent-vpn-state")
        self.emit = emit or (lambda event, **fields: print(json.dumps({"event": event, **fields}), flush=True))

    def save_control(self, state, control, extra=None):
        data = {"failover.json": b64(json.dumps(control).encode()), **(extra or {})}
        return self.api.patch(self.state_name, data, state["metadata"]["resourceVersion"])

    def step(self, health, now):
        state = self.api.get(self.state_name)
        if state.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") != "aof-agent-vpn":
            raise ValueError("state Secret is not owned by this VPN component")
        if not eligible(health, state, now, self.reconnects):
            return False
        control = control_state(state)
        source = decode_key(decode_field(self.api.get(self.source_name), "connection-key"))
        identity = json.loads(decode_field(state, "identity.json"))
        payload = config_payload(source, identity)
        if control.get("phase") != "provisioning":
            countries = request("v1/account_info", payload).get("available_countries", [])
            country = next_country(countries, control.get("country"))
            if not country:
                self.emit("no_alternative_country")
                self.save_control(state, {**control, "next_action_at": now + self.cooldown})
                return False
            control = {**control, "phase": "provisioning", "target_country": country,
                       "operation_id": str(uuid.uuid4()), "base_config_id": digest(decode_field(state, "primary.conf")),
                       "next_action_at": now}
            # Durable intent precedes the external request. A restarted watchdog
            # retries this same country and device identity, never a fresh device.
            state = self.save_control(state, control)
            self.emit("country_change_requested", country=country,
                      failed_attempts=health.get("failed_attempts", 0))
        if digest(decode_field(state, "primary.conf")) != control["base_config_id"]:
            self.save_control(state, {**control, "phase": "blocked"})
            self.emit("manual_configuration_change_detected")
            return False
        try:
            payload["server_country_code"] = control["target_country"]
            response = request("v1/config", payload)
            native, _ = native_configuration(response, identity)
            applied = {"phase": "applied", "country": control["target_country"],
                       "generation": control["operation_id"], "switched_at": now,
                       "next_action_at": now + self.cooldown}
            # Never overwrite an operator's concurrent configuration or identity.
            # Retrying this local CAS does not repeat provider provisioning.
            for _ in range(3):
                latest = self.api.get(self.state_name)
                latest_control = control_state(latest)
                if (latest_control.get("operation_id") != control["operation_id"]
                        or decode_field(latest, "identity.json") != decode_field(state, "identity.json")
                        or digest(decode_field(latest, "primary.conf")) != control["base_config_id"]):
                    raise Conflict()
                try:
                    self.save_control(latest, applied, {"primary.conf": b64(native.encode())})
                    break
                except Conflict:
                    continue
            else:
                raise Conflict()
            self.emit("country_configuration_applied", country=applied["country"])
            return True
        except Exception as error:
            latest = self.api.get(self.state_name)
            current = control_state(latest)
            if current.get("operation_id") == control["operation_id"]:
                self.save_control(latest, {**current, "next_action_at": now + 60})
            self.emit("country_change_failed", reason=type(error).__name__)
            return False


def health(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read(16384))
    except (OSError, ValueError):
        return None


def run():
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    watchdog = Watchdog(Kubernetes(), cooldown=int(os.environ.get("COUNTRY_COOLDOWN_SECONDS", "300")),
                        reconnects=int(os.environ.get("RECONNECT_ATTEMPTS", "2")))
    url = os.environ["VPN_HEALTH_URL"]
    while not stopped.is_set():
        HEARTBEAT.write_text(str(time.time()))
        try:
            watchdog.step(health(url), time.time())
        except Exception as error:
            watchdog.emit("watchdog_error", reason=type(error).__name__)
        stopped.wait(15)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "live":
        try:
            sys.exit(0 if 0 <= time.time() - float(HEARTBEAT.read_text()) < 180 else 1)
        except (OSError, ValueError):
            sys.exit(1)
    run()

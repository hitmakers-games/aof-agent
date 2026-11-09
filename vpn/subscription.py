"""Provision one reusable VPN identity from an Amnezia subscription Secret."""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request
import uuid

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding, x25519
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from connection_key import decode_key
from vpn import parse_config

GATEWAY = "https://gw.amnezia.org/"
MAX_RESPONSE = 1024 * 1024


def b64(value):
    return base64.b64encode(value).decode()


def encrypt_request(payload, public_key):
    key, iv, salt = os.urandom(32), os.urandom(32), os.urandom(8)
    material = json.dumps({"aes_key": b64(key), "aes_iv": b64(iv), "aes_salt": b64(salt)}, separators=(",", ":")).encode()
    wrapped = serialization.load_pem_public_key(public_key).encrypt(material, rsa_padding.PKCS1v15())
    padder = padding.PKCS7(128).padder()
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    plaintext = padder.update(encoded) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv[:16])).encryptor()
    encrypted = cipher.update(plaintext) + cipher.finalize()
    return json.dumps({"key_payload": b64(wrapped), "api_payload": b64(encrypted)}).encode(), key, iv


def decrypt_response(response, key, iv):
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv[:16])).decryptor()
    plaintext = cipher.update(response) + cipher.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return json.loads(unpadder.update(plaintext) + unpadder.finalize())


def request(endpoint, payload):
    public_key = Path(__file__).with_name("gateway-public.pem").read_bytes()
    body, key, iv = encrypt_request(payload, public_key)
    req = urllib.request.Request(GATEWAY + endpoint, data=body, headers={
        "Content-Type": "application/json", "X-Client-Request-ID": str(uuid.uuid4())
    })
    try:
        response = urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError("provider response is too large")
    result = decrypt_response(raw, key, iv)
    status = result.get("http_status")
    if type(status) is not int or not 200 <= status < 300:
        # Provider error bodies may contain account details; publish only the code.
        raise ValueError(f"provider API status {status if type(status) is int else 'invalid'}")
    return result


def new_identity():
    key = x25519.X25519PrivateKey.generate()
    return {
        "private_key": b64(key.private_bytes_raw()),
        "public_key": b64(key.public_key().public_bytes_raw()),
        "installation_uuid": str(uuid.uuid4()),
    }


def config_payload(config, identity, country=None):
    api = config["api_config"]
    payload = {
        "service_type": api["service_type"], "service_protocol": "awg",
        "user_country_code": api["user_country_code"], "auth_data": config["auth_data"],
        "public_key": identity["public_key"], "installation_uuid": identity["installation_uuid"],
        "os_version": "linux", "app_version": "5.1.0.6", "cli_name": "aof-agent-vpn",
        "distribution": "github", "app_language": "en",
    }
    if country:
        payload["server_country_code"] = country
    return payload


def native_configuration(response, identity):
    data = decode_key(response["config"])
    candidates = []
    for container in data.get("containers", []):
        for protocol in container.values():
            if not isinstance(protocol, dict) or "last_config" not in protocol:
                continue
            last = protocol["last_config"]
            if isinstance(last, str):
                last = json.loads(last)
            if isinstance(last, dict) and isinstance(last.get("config"), str):
                candidates.append(last["config"])
    if len(candidates) != 1:
        raise ValueError("provider did not return one native VPN configuration")
    text = candidates[0].replace("$WIREGUARD_CLIENT_PRIVATE_KEY", identity["private_key"])
    settings, addresses, mtu, endpoint = parse_config(text)
    # Ensure the provider response cannot silently replace our stable identity.
    private_lines = [line.split("=", 1)[1].strip() for line in text.splitlines()
                     if line.strip().startswith("PrivateKey") and "=" in line]
    if private_lines != [identity["private_key"]]:
        raise ValueError("provider configuration identity mismatch")
    native = "[Interface]\nAddress = " + ", ".join(addresses) + f"\nMTU = {mtu}\n"
    native += settings.removeprefix("[Interface]\n")
    return native, endpoint


class Secrets:
    def __init__(self, context, namespace):
        self.base = ["kubectl", "--context", context, "--request-timeout=20s", "-n", namespace]

    def get(self, name):
        result = subprocess.run(self.base + ["get", "secret", name, "--ignore-not-found", "-o", "json"],
                                capture_output=True, text=True, timeout=25, check=True)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def create(self, name, data):
        document = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": {
            "name": name, "labels": {"app.kubernetes.io/name": "aof-agent-vpn"}}, "data": data}
        subprocess.run(self.base + ["create", "-f", "-"], input=json.dumps(document),
                       capture_output=True, text=True, timeout=25, check=True)

    def update(self, name, data):
        # The patch travels on stdin, never in process arguments or temporary files.
        subprocess.run(self.base + ["patch", "secret", name, "--type=merge", "--patch-file=/dev/stdin"],
                       input=json.dumps({"data": data}), capture_output=True, text=True, timeout=25, check=True)


def provision(context, namespace, source_name, state_name, refresh=False):
    secrets = Secrets(context, namespace)
    source = secrets.get(source_name)
    if not source:
        raise ValueError("subscription Secret is missing")
    code = base64.b64decode(source["data"]["connection-key"]).decode()
    config = decode_key(code)
    api = config["api_config"]
    if api.get("service_protocol") != "awg" or api.get("service_type") not in ("amnezia-premium", "external-premium"):
        raise ValueError("this provisioning flow requires a Premium AmneziaWG subscription")
    state = secrets.get(state_name)
    if state:
        if state.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") != "aof-agent-vpn":
            raise ValueError("refusing to change a Secret not owned by this VPN component")
        if "primary.conf" in state.get("data", {}) and not refresh:
            print("VPN configuration already provisioned; existing device identity preserved.")
            return
        identity = json.loads(base64.b64decode(state["data"]["identity.json"]))
    else:
        identity = new_identity()
        secrets.create(state_name, {"identity.json": b64(json.dumps(identity).encode())})
    payload = config_payload(config, identity)
    response = request("v1/config", payload)
    text, _ = native_configuration(response, identity)
    secrets.update(state_name, {"primary.conf": b64(text.encode())})
    print("VPN configuration provisioned into its dedicated state Secret; credentials were not displayed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="aof-agent-network")
    parser.add_argument("--source-secret", default="aof-agent-vpn-config")
    parser.add_argument("--state-secret", default="aof-agent-vpn-state")
    parser.add_argument("--refresh", action="store_true", help="Refresh configuration using the existing device identity")
    args = parser.parse_args()
    try:
        provision(args.context, args.namespace, args.source_secret, args.state_secret, args.refresh)
    except Exception as error:
        # Never print exception objects: HTTP, JSON or subprocess errors may contain secrets.
        print(f"Provisioning failed ({type(error).__name__}); credentials were not displayed.", file=__import__('sys').stderr)
        raise SystemExit(1)

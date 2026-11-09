"""Offline Amnezia key inspection. Never print decoded credentials or endpoints."""
import base64
import json
import sys
import zlib

LIMIT = 1024 * 1024


def decode_key(value):
    value = value.strip()
    if len(value) > LIMIT or not value.startswith("vpn://"):
        raise ValueError("unsupported connection key")
    encoded = value[6:]
    payload = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    if not payload.lstrip().startswith(b"{"):
        if len(payload) <= 4 or payload[4] != 0x78:
            raise ValueError("invalid connection key encoding")
        # Qt qCompress prefixes the zlib stream with its big-endian original size.
        expected = int.from_bytes(payload[:4], "big")
        if expected > LIMIT:
            raise ValueError("oversized connection key")
        stream = zlib.decompressobj()
        payload = stream.decompress(payload[4:], LIMIT + 1)
        # Subscription links use a fixed 0x000000ff marker, not always the size.
        if not stream.eof or stream.unused_data or len(payload) > LIMIT:
            raise ValueError("invalid compressed connection key")
    result = json.loads(payload)
    if not isinstance(result, dict):
        raise ValueError("unsupported connection key payload")
    return result


def inspect_key(value):
    if value.strip().startswith("vless://"):
        return {"format": "vless", "requires": "Xray-compatible client"}
    if value.strip().startswith("ss://"):
        return {"format": "shadowsocks", "requires": "Shadowsocks-compatible client"}
    data = decode_key(value)
    api = data.get("api_config")
    subscription = isinstance(api, dict) or "api_endpoint" in data
    containers = data.get("containers")
    version = data.get("config_version")
    return {
        "format": "amnezia-subscription" if subscription else "amnezia-connection",
        "config_version": version if type(version) is int else None,
        "contains_connection_settings": isinstance(containers, list) and bool(containers),
        "requires_provider_provisioning": subscription and not bool(containers),
    }


if __name__ == "__main__":
    try:
        value = sys.stdin.read(LIMIT + 1)
        print(json.dumps(inspect_key(value)))
    except (ValueError, TypeError, KeyError, zlib.error):
        print("Unsupported or invalid connection code; no credential values were displayed.", file=sys.stderr)
        sys.exit(1)

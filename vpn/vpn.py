"""AmneziaWG supervisor with persistent egress blocking and bounded recovery."""
import ipaddress
import hashlib
import json
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

INTERFACE = "awg0"
STATUS = Path("/status/health.json")
RUNTIME = Path("/run/vpn")
STOP = False
HEALTH_CONTEXT = {}


class ConfigurationError(ValueError):
    pass


class ConfigurationChanged(Exception):
    pass


def configuration_snapshot(path):
    # Resolve Kubernetes' atomic Secret projection once for a coherent snapshot.
    projection = path.parent / "..data"
    directory = projection.resolve() if projection.is_symlink() else path.parent
    text = (directory / path.name).read_text()
    metadata = directory / "failover.json"
    control = json.loads(metadata.read_text()) if metadata.exists() else {}
    return text, hashlib.sha256(text.encode()).hexdigest(), control


def configuration_changed(path, config_id, generation):
    try:
        _, current_id, control = configuration_snapshot(path)
        return current_id != config_id or control.get("generation") != generation
    except (OSError, ValueError):
        return False


def parse_config(text):
    """Separate address settings without executing wg-quick hooks or logging keys."""
    section = None
    sections = []
    lines, addresses = [], []
    endpoint = None
    mtu = 1280
    allowed_default = False
    private_key = public_key = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line in ("[Interface]", "[Peer]"):
            section = line
            sections.append(section)
            lines.append(line)
            continue
        if "=" not in line or section is None:
            raise ConfigurationError("invalid configuration structure")
        key, value = (s.strip() for s in line.split("=", 1))
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key) or not value:
            raise ConfigurationError("invalid configuration setting")
        if key.lower() in {"preup", "postup", "predown", "postdown", "saveconfig", "table", "fwmark"}:
            raise ConfigurationError("configuration contains unsupported routing or executable settings")
        if section == "[Interface]" and key == "ListenPort":
            if not 0 <= int(value) <= 65535:
                raise ConfigurationError("invalid listen port")
            # Client-only gateway: let the OS allocate the local UDP port.
            continue
        if section == "[Interface]" and key == "Address":
            for address in value.split(","):
                parsed = ipaddress.ip_interface(address.strip())
                if parsed.version == 4:
                    addresses.append(str(parsed))
            continue
        if section == "[Interface]" and key == "DNS":
            # Pod DNS is configured explicitly; DNS queries travel inside the tunnel.
            continue
        if section == "[Interface]" and key == "MTU":
            mtu = int(value)
            if not 576 <= mtu <= 1420:
                raise ConfigurationError("unsupported MTU")
            continue
        if key == "Endpoint" and section == "[Peer]":
            host, port = value.rsplit(":", 1)
            parsed = ipaddress.ip_address(host)
            if parsed.version != 4 or parsed.is_unspecified or parsed.is_multicast or parsed.is_loopback or parsed.is_link_local:
                raise ConfigurationError("a numeric IPv4 VPN endpoint is required")
            port = int(port)
            if not 1 <= port <= 65535 or endpoint is not None:
                raise ConfigurationError("one VPN endpoint is required")
            endpoint = (str(parsed), port)
        if key == "AllowedIPs" and section == "[Peer]":
            networks = [ipaddress.ip_network(n.strip()) for n in value.split(",")]
            allowed_default = ipaddress.ip_network("0.0.0.0/0") in networks
            # This gateway deliberately supports IPv4 only; IPv6 stays blocked.
            value = ", ".join(str(n) for n in networks if n.version == 4)
        if key == "PersistentKeepalive" and section == "[Peer]":
            continue
        private_key |= section == "[Interface]" and key == "PrivateKey"
        public_key |= section == "[Peer]" and key == "PublicKey"
        lines.append(f"{key} = {value}")
    if sections != ["[Interface]", "[Peer]"] or not all((endpoint, addresses, allowed_default, private_key, public_key)):
        raise ConfigurationError("one interface and one full-tunnel IPv4 peer are required")
    lines.append("PersistentKeepalive = 25")
    return "\n".join(lines) + "\n", addresses, mtu, endpoint


def command(*args, input=None):
    return subprocess.run(args, input=input, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=15, check=True).stdout.strip()


def log(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


def status(ready=False, **fields):
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps({**HEALTH_CONTEXT, "heartbeat": time.time(), "ready": ready, **fields}))
    temporary.chmod(0o644)
    temporary.replace(STATUS)


def check_health(kind):
    try:
        data = json.loads(STATUS.read_text())
        fresh = 0 <= time.time() - data["heartbeat"] < 90
        return fresh and (kind == "live" or data["ready"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def wait(seconds, changed=None):
    until = time.monotonic() + seconds
    while not STOP and time.monotonic() < until:
        if changed and changed():
            return
        status(phase="backoff")
        time.sleep(min(1, max(0, until - time.monotonic())))


def firewall(endpoint=None):
    # Policies are installed BEFORE any tunnel or proxy can originate traffic.
    # The dedicated chain is atomically replaced; its final REJECT survives reconnects.
    rules = ["*filter", ":OUTPUT DROP [0:0]", ":AOF_VPN - [0:0]", "-F AOF_VPN",
             "-A AOF_VPN -o lo -j ACCEPT",
             "-A AOF_VPN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
             "-A AOF_VPN -o awg0 -j ACCEPT"]
    if endpoint:
        host, port = endpoint
        rules.append(f"-A AOF_VPN -d {host}/32 -p udp --dport {port} -m mark --mark 51820 -j ACCEPT")
    rules.extend(["-A AOF_VPN -j REJECT", "-A OUTPUT -j AOF_VPN", "COMMIT"])
    command("iptables-restore", input="\n".join(rules) + "\n")
    command("ip6tables", "-P", "OUTPUT", "DROP")


def stop_tunnel(process):
    if process is not None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    try:
        command("ip", "link", "delete", INTERFACE)
    except subprocess.SubprocessError:
        pass


def routing(device):
    addresses = json.loads(command("ip", "-j", "-4", "address", "show", "dev", device))
    source = next(a["local"] for link in addresses for a in link["addr_info"] if a.get("scope") == "global")
    # Keep the original main table for encrypted UDP and replies to proxy clients.
    # New outbound connections select the tunnel table and its tunnel source IP.
    rules = (
        (10518, ("fwmark", "51820", "lookup", "main")),
        (10519, ("from", source + "/32", "lookup", "main")),
        (10520, ("lookup", "51820")),
    )
    for priority, arguments in rules:
        try:
            command("ip", "-4", "rule", "delete", "priority", str(priority))
        except subprocess.CalledProcessError:
            pass
        command("ip", "-4", "rule", "add", "priority", str(priority), *arguments)


def endpoint_routing(endpoint):
    # The first UDP socket must choose the pod source address even before fwmark
    # is applied; otherwise its cached source can be the tunnel's private IP.
    try:
        command("ip", "-4", "rule", "delete", "priority", "10517")
    except subprocess.CalledProcessError:
        pass
    command("ip", "-4", "rule", "add", "priority", "10517", "to", endpoint[0] + "/32", "lookup", "main")


def probes():
    urls = os.environ.get("PROBE_URLS", "https://api.openai.com/v1/models,https://api.telegram.org/").split(",")
    results = {}
    for url in urls:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ConfigurationError("HTTPS probes without credentials are required")
        try:
            code = command("curl", "--silent", "--show-error", "--proxy", "http://127.0.0.1:8888",
                           "--noproxy", "", "--connect-timeout", "4", "--max-time", "8",
                           "--output", "/dev/null", "--write-out", "%{http_code}", url)
            # Authentication and rate limits are reachable services. Forbidden
            # regions and persistent server errors should trigger recovery.
            results[parsed.hostname] = code.isdigit() and (200 <= int(code) < 400 or int(code) in (401, 429))
        except subprocess.SubprocessError:
            results[parsed.hostname] = False
    return results


def handshake_recent():
    try:
        timestamp = int(command("awg", "show", INTERFACE, "latest-handshakes").split()[-1])
        return 0 <= time.time() - timestamp < 180
    except (subprocess.SubprocessError, ValueError, IndexError):
        return False


def egress_ip():
    try:
        value = command("curl", "--silent", "--proxy", "http://127.0.0.1:8888", "--noproxy", "",
                        "--connect-timeout", "4", "--max-time", "8", "https://api.ipify.org")
        return str(ipaddress.IPv4Address(value))
    except (subprocess.SubprocessError, ValueError):
        return None


def run():
    global STOP
    signal.signal(signal.SIGTERM, lambda *_: stop())
    signal.signal(signal.SIGINT, lambda *_: stop())
    RUNTIME.mkdir(parents=True, exist_ok=True)
    RUNTIME.chmod(0o700)
    route = json.loads(command("ip", "-j", "route", "show", "default"))[0]
    device = route["dev"]
    firewall()
    routing(device)
    status(phase="starting")
    failures, selection = 0, 0
    paths = [Path("/config/primary.conf")]
    if os.environ.get("AUTO_COUNTRY_FAILOVER") != "1" and Path("/config/backup.conf").exists():
        paths.append(Path("/config/backup.conf"))
    process = None
    try:
        while not STOP:
            profile = "primary" if selection == 0 else "backup"
            status(phase="connecting", profile=profile)
            changed = None
            try:
                text, config_id, control = configuration_snapshot(paths[selection])
                generation = control.get("generation")
                HEALTH_CONTEXT.update(config_id=config_id, generation=generation,
                                      country=control.get("country", "automatic"), failed_attempts=failures)
                changed = lambda: configuration_changed(paths[selection], config_id, generation)
                configuration, addresses, mtu, endpoint = parse_config(text)
                endpoint_routing(endpoint)
                firewall(endpoint)
                process = subprocess.Popen(["amneziawg-go", "--foreground", INTERFACE],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(50):
                    if Path("/sys/class/net", INTERFACE).exists():
                        break
                    if process.poll() is not None:
                        raise RuntimeError("tunnel process stopped")
                    time.sleep(0.1)
                active = RUNTIME / "active.conf"
                active.write_text(configuration)
                active.chmod(0o600)
                command("awg", "setconf", INTERFACE, str(active))
                command("awg", "set", INTERFACE, "fwmark", "51820")
                active.unlink()
                for address in addresses:
                    command("ip", "address", "add", address, "dev", INTERFACE)
                command("ip", "link", "set", INTERFACE, "mtu", str(mtu), "up")
                command("ip", "-4", "route", "replace", "default", "dev", INTERFACE, "table", "51820")
                bad, good, connected, public_ip = 0, 0, False, None
                while not STOP:
                    if changed():
                        raise ConfigurationChanged()
                    if process.poll() is not None:
                        raise RuntimeError("tunnel process stopped")
                    result = probes()
                    ready = handshake_recent() and bool(result) and all(result.values())
                    if ready:
                        bad = 0
                        good += 1
                        if good >= 2:
                            failures = 0
                            HEALTH_CONTEXT["failed_attempts"] = 0
                        if not connected:
                            public_ip = egress_ip()
                            log("connected", profile=profile, egress_ip=public_ip)
                            connected = True
                    else:
                        bad += 1
                        good = 0
                    status(ready, phase="connected" if ready else "degraded", profile=profile,
                           probes=result, egress_ip=public_ip)
                    if bad >= 3:
                        raise RuntimeError("repeated connectivity failure")
                    for _ in range(15):
                        if STOP:
                            break
                        time.sleep(1)
            except ConfigurationChanged:
                failures = 0
                HEALTH_CONTEXT["failed_attempts"] = 0
                log("configuration_changed")
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                # Exception text can contain configuration material; only publish its type.
                log("reconnecting", profile=profile, reason=type(exc).__name__)
                failures += 1
                HEALTH_CONTEXT["failed_attempts"] = failures
            finally:
                status(phase="disconnected", profile=profile)
                stop_tunnel(process)
                process = None
                (RUNTIME / "active.conf").unlink(missing_ok=True)
            if failures >= 2 and len(paths) > 1:
                selection = (selection + 1) % len(paths)
                failures = 0
                log("switching_profile", profile="primary" if selection == 0 else "backup")
            wait(min(60, 5 * 2 ** min(failures, 4)) + random.uniform(0, 3), changed)
    finally:
        stop_tunnel(process)
        status(phase="stopped")


def stop():
    global STOP
    STOP = True


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode in ("ready", "live"):
        sys.exit(0 if check_health(mode) else 1)
    if mode == "config-check":
        try:
            parse_config(Path(sys.argv[2]).read_text())
            print("Configuration structure is supported; keys and server compatibility require a connection test.")
        except (OSError, ValueError):
            print("Unsupported configuration. Requires one IPv4 full-tunnel AmneziaWG peer, without hooks.", file=sys.stderr)
            sys.exit(1)
    elif mode == "run":
        run()
    else:
        sys.exit("Unknown mode")

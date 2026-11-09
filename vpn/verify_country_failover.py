"""Bounded, explicitly enabled live outage test for the dedicated VPN gateway."""
import argparse
import hashlib
import ipaddress
import json
import subprocess
import time
import uuid


INSTALL = '''
import json,subprocess,sys
from pathlib import Path
sys.path.insert(0,"/app");import vpn
record=Path("/run/vpn/country-failover-test.json")
if record.exists():raise SystemExit("A previous fault record exists; refusing a second injection.")
endpoint=vpn.parse_config(Path("/config/primary.conf").read_text())[3]
record.write_text(json.dumps({"operation_id":"__OPERATION__","endpoint":endpoint}));record.chmod(0o600)
rule=["iptables","-t","mangle","-I","OUTPUT","1","-p","udp","-d",endpoint[0],"--dport",str(endpoint[1]),"-m","comment","--comment","aof-vpn-country-test","-j","DROP"]
subprocess.run(rule,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print("Blocked only this VPN pod's current encrypted UDP endpoint.")
'''

RESTORE = '''
import json,subprocess
from pathlib import Path
record=Path("/run/vpn/country-failover-test.json")
if record.exists():
 data=json.loads(record.read_text())
 if not isinstance(data,dict) or data.get("operation_id")!="__OPERATION__":
  raise SystemExit("Fault record belongs to another test; preserving it.")
 endpoint=data["endpoint"]
 rule=["iptables","-t","mangle","-D","OUTPUT","-p","udp","-d",endpoint[0],"--dport",str(endpoint[1]),"-m","comment","--comment","aof-vpn-country-test","-j","DROP"]
 subprocess.run(rule,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 record.unlink()
print("Temporary VPN fault removed.")
'''


def verify(context, namespace, timeout):
    base = ["kubectl", "--context", context, "--request-timeout=20s", "-n", namespace]

    def kubectl(*args, data=None):
        result = subprocess.run(base + list(args), input=data, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Namespaced verification command failed; sensitive output suppressed")
        return result.stdout

    pods = json.loads(kubectl("get", "pods", "-l", "app.kubernetes.io/instance=aof-agent-vpn", "-o", "json"))["items"]
    if len(pods) != 1:
        raise RuntimeError("Expected exactly one VPN gateway pod")
    pod, uid = pods[0]["metadata"]["name"], pods[0]["metadata"]["uid"]

    def health():
        return json.loads(kubectl("exec", pod, "-c", "vpn", "--", "cat", "/status/health.json"))

    def identity():
        state = json.loads(kubectl("get", "secret", "aof-agent-vpn-state", "-o", "json"))
        return hashlib.sha256(state["data"]["identity.json"].encode()).hexdigest()

    original = health()
    if not original.get("ready"):
        raise RuntimeError("VPN must be healthy before starting the outage test")
    original_identity = identity()
    operation = uuid.uuid4().hex
    seen_reconnect = False
    previous = None
    try:
        print(kubectl("exec", "-i", pod, "-c", "vpn", "--", "python3", "-", data=INSTALL.replace("__OPERATION__", operation)).strip(), flush=True)
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            current = health()
            failed = current.get("failed_attempts", 0)
            seen_reconnect |= failed >= 2
            summary = {k: current.get(k) for k in ("phase", "country", "failed_attempts", "ready")}
            if summary != previous:
                print(json.dumps(summary), flush=True)
                previous = summary
            if (current.get("ready") and current.get("generation") != original.get("generation")
                    and current.get("country") != original.get("country")):
                if not seen_reconnect:
                    raise RuntimeError("Country changed without observing the failed reconnect")
                if identity() != original_identity:
                    raise RuntimeError("Device identity unexpectedly changed")
                try:
                    valid_egress = ipaddress.IPv4Address(current.get("egress_ip")).is_global
                except (ValueError, TypeError):
                    valid_egress = False
                if not valid_egress or current.get("egress_ip") == original.get("egress_ip"):
                    raise RuntimeError("Country changed but a different egress address was not observed")
                print(json.dumps({"passed": True, "reconnect_before_country_change": True,
                                  "device_identity_preserved": True, "egress_changed": True,
                                  "country": current["country"], "probes": current.get("probes")}))
                return
            time.sleep(5)
        raise TimeoutError("Country failover did not recover before the bounded test timeout")
    finally:
        # If Kubernetes replaced the pod, its old network namespace and rule are gone.
        remaining = json.loads(kubectl("get", "pods", "-l", "app.kubernetes.io/instance=aof-agent-vpn", "-o", "json"))["items"]
        if any(p["metadata"]["uid"] == uid for p in remaining):
            print(kubectl("exec", "-i", pod, "-c", "vpn", "--", "python3", "-", data=RESTORE.replace("__OPERATION__", operation)).strip(), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="aof-agent-network")
    parser.add_argument("--timeout", type=int, default=480)
    parser.add_argument("--inject-outage", action="store_true", required=True,
                        help="Explicitly permit the temporary pod-local UDP outage")
    args = parser.parse_args()
    verify(args.context, args.namespace, args.timeout)

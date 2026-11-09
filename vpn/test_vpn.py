import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

import proxy
import vpn


CONFIG = """[Interface]
PrivateKey = SYNTHETIC_PRIVATE_KEY
Address = 10.77.0.2/32, fd00::2/128
ListenPort = 51820
DNS = 1.1.1.1
Jc = 4
MTU = 1280
[Peer]
PublicKey = SYNTHETIC_PUBLIC_KEY
Endpoint = 203.0.113.10:51820
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 0
"""


class ConfigurationTests(unittest.TestCase):
    def test_atomic_projection_and_generation_trigger_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version = root / "version1"
            version.mkdir()
            (version / "primary.conf").write_text(CONFIG)
            (version / "failover.json").write_text(json.dumps({"country": "de", "generation": "one"}))
            (root / "..data").symlink_to(version.name)
            path = root / "primary.conf"
            text, config_id, control = vpn.configuration_snapshot(path)
            self.assertEqual(text, CONFIG)
            self.assertFalse(vpn.configuration_changed(path, config_id, "one"))
            (version / "failover.json").write_text(json.dumps({"country": "nl", "generation": "two"}))
            self.assertTrue(vpn.configuration_changed(path, config_id, "one"))
            with patch.object(vpn.time, "sleep") as sleep:
                vpn.wait(60, lambda: vpn.configuration_changed(path, config_id, "one"))
                sleep.assert_not_called()

    def test_native_configuration_preserves_awg_parameters_without_routes_or_dns(self):
        text, addresses, mtu, endpoint = vpn.parse_config(CONFIG)
        self.assertEqual(addresses, ["10.77.0.2/32"])
        self.assertEqual(endpoint, ("203.0.113.10", 51820))
        self.assertEqual(mtu, 1280)
        self.assertIn("Jc = 4", text)
        self.assertNotIn("DNS", text)
        self.assertNotIn("ListenPort", text)
        self.assertNotIn("::/0", text)
        self.assertEqual(text.count("PersistentKeepalive"), 1)
        self.assertIn("PersistentKeepalive = 25", text)

    def test_executable_hooks_and_routing_overrides_are_rejected(self):
        for setting in ("PostUp = echo SECRET", "PreDown = echo SECRET", "SaveConfig = true", "Table = main", "FwMark = 0"):
            with self.subTest(setting=setting), self.assertRaises(vpn.ConfigurationError):
                vpn.parse_config(CONFIG.replace("Jc = 4", setting))

    def test_incomplete_or_ambiguous_configs_are_rejected(self):
        for invalid in (
            CONFIG.replace("0.0.0.0/0", "10.0.0.0/8"),
            CONFIG.replace("203.0.113.10", "vpn.example.com"),
            CONFIG.replace("203.0.113.10", "127.0.0.1"),
            CONFIG.replace("51820", "65536"),
            CONFIG + "[Peer]\nPublicKey = extra\n",
            CONFIG.replace("[Interface]", "[SomethingElse]"),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                vpn.parse_config(invalid)


class SafetyTests(unittest.TestCase):
    def test_endpoint_route_is_available_before_udp_socket_marking(self):
        with patch.object(vpn, "command") as command:
            vpn.endpoint_routing(("203.0.113.10", 51820))
        self.assertEqual(command.call_args.args, ("ip", "-4", "rule", "add", "priority", "10517", "to", "203.0.113.10/32", "lookup", "main"))

    def test_proxy_replies_and_encrypted_udp_keep_the_original_route(self):
        address = json.dumps([{"addr_info": [{"local": "10.10.1.72", "scope": "global"}]}])
        with patch.object(vpn, "command", return_value=address) as command:
            vpn.routing("eth0")
        calls = [call.args for call in command.call_args_list]
        self.assertIn(("ip", "-4", "rule", "add", "priority", "10518", "fwmark", "51820", "lookup", "main"), calls)
        self.assertIn(("ip", "-4", "rule", "add", "priority", "10519", "from", "10.10.1.72/32", "lookup", "main"), calls)
        self.assertIn(("ip", "-4", "rule", "add", "priority", "10520", "lookup", "51820"), calls)
        self.assertFalse(any("route" in call and "replace" in call for call in calls))

    def test_firewall_blocks_egress_without_tunnel_and_only_permits_marked_endpoint(self):
        with patch.object(vpn, "command") as command:
            vpn.firewall(("203.0.113.10", 51820))
        rules = command.call_args_list[0].kwargs["input"]
        self.assertIn(":OUTPUT DROP", rules)
        self.assertIn("-o awg0 -j ACCEPT", rules)
        self.assertIn("-d 203.0.113.10/32 -p udp --dport 51820 -m mark --mark 51820", rules)
        self.assertTrue(rules.endswith("-A AOF_VPN -j REJECT\n-A OUTPUT -j AOF_VPN\nCOMMIT\n"))
        self.assertEqual(command.call_args_list[1].args, ("ip6tables", "-P", "OUTPUT", "DROP"))

    def test_health_rejects_stale_or_partial_evidence(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(vpn, "STATUS", Path(directory) / "health.json"):
            self.assertFalse(vpn.check_health("ready"))
            vpn.status(False, phase="degraded")
            self.assertTrue(vpn.check_health("live"))
            self.assertFalse(vpn.check_health("ready"))
            vpn.status(True)
            self.assertTrue(vpn.check_health("ready"))
            vpn.STATUS.write_text(json.dumps({"heartbeat": time.time() - 100, "ready": True}))
            self.assertFalse(vpn.check_health("live"))

    def test_http_auth_failure_still_proves_transport(self):
        with patch.object(vpn, "command", return_value="401"):
            self.assertTrue(all(vpn.probes().values()))

    def test_http_connection_failure_is_not_readiness(self):
        with patch.object(vpn, "command", return_value="000"):
            self.assertFalse(any(vpn.probes().values()))

    def test_forbidden_regions_and_server_errors_trigger_recovery(self):
        for status in ("403", "500", "503"):
            with self.subTest(status=status), patch.object(vpn, "command", return_value=status):
                self.assertFalse(any(vpn.probes().values()))
        with patch.object(vpn, "command", return_value="429"):
            self.assertTrue(all(vpn.probes().values()))


class ProxyTests(unittest.TestCase):
    def test_health_endpoint_never_exposes_extra_secret_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "status.json"
            status.write_text(json.dumps({"ready": False, "failed_attempts": 2, "private_key": "SYNTHETIC_SECRET"}))
            with patch.object(proxy, "STATUS", status), proxy.ThreadingHTTPServer(("127.0.0.1", 0), proxy.Health) as server:
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=2) as response:
                    body = response.read().decode()
                    self.assertNotIn("SYNTHETIC_SECRET", body)
                    self.assertEqual(json.loads(body), {"ready": False, "failed_attempts": 2})
                server.shutdown()
                worker.join(timeout=2)

    def test_disallows_other_ports_hosts_and_private_dns_answers(self):
        for authority in ("api.openai.com:80", "evil.example:443", "127.0.0.1:443", "api.openai.com.evil:443"):
            with self.subTest(authority=authority), self.assertRaises(ValueError):
                proxy.destination(authority)
        with patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 443))]):
            with self.assertRaises(ValueError):
                proxy.destination("api.openai.com:443")

    def test_connect_request_relay_and_host_denial(self):
        # Real TCP proxy and echo upstream; only DNS validation is substituted.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            def echo():
                connection, _ = listener.accept()
                with connection:
                    connection.sendall(connection.recv(1024))
            upstream = threading.Thread(target=echo, daemon=True)
            upstream.start()
            with proxy.Server(("127.0.0.1", 0)) as server:
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                with patch.object(proxy, "destination", return_value=listener.getsockname()):
                    with socket.create_connection(server.server_address) as client:
                        client.settimeout(2)
                        client.sendall(b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com\r\n\r\n")
                        self.assertIn(b"200 Connection Established", client.recv(4096))
                        client.sendall(b"TLS_BYTES")
                        self.assertEqual(client.recv(4096), b"TLS_BYTES")
                with socket.create_connection(server.server_address) as client:
                    client.settimeout(2)
                    client.sendall(b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n")
                    self.assertIn(b"403 Forbidden", client.recv(4096))
                server.shutdown()
                worker.join(timeout=2)
            upstream.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

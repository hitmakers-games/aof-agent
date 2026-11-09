import copy
import json
import unittest
from unittest.mock import patch

import failover
from subscription import b64, new_identity
from test_connection_key import key


class API:
    def __init__(self):
        self.identity = new_identity()
        self.state = {"metadata": {"resourceVersion": "1", "labels": {"app.kubernetes.io/name": "aof-agent-vpn"}},
                      "data": {"identity.json": b64(json.dumps(self.identity).encode()), "primary.conf": b64(b"ORIGINAL")}}
        self.source = {"data": {"connection-key": b64(key({"api_config": {"service_type": "external-premium", "service_protocol": "awg", "user_country_code": "test"}, "auth_data": {"api_key": "SYNTHETIC"}}).encode())}}
        self.operations = []

    def get(self, name):
        return copy.deepcopy(self.state if name == "aof-agent-vpn-state" else self.source)

    def patch(self, name, data, version):
        if name != "aof-agent-vpn-state" or version != self.state["metadata"]["resourceVersion"]:
            raise failover.Conflict()
        self.operations.append(copy.deepcopy(data))
        self.state["data"].update(data)
        self.state["metadata"]["resourceVersion"] = str(int(version) + 1)
        return copy.deepcopy(self.state)

    def health(self, now=1000, attempts=2, ready=False):
        control = failover.control_state(self.state)
        return {"heartbeat": now, "failed_attempts": attempts, "ready": ready,
                "config_id": failover.digest(failover.decode_field(self.state, "primary.conf")),
                "generation": control.get("generation")}


COUNTRIES = [{"server_country_code": c, "available_protocols": ["awg"]} for c in ("gl", "de", "nl")]


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.events = []
        self.worker = failover.Watchdog(self.api, emit=lambda event, **fields: self.events.append((event, fields)))

    def provider(self, endpoint, payload):
        if endpoint == "v1/account_info":
            return {"available_countries": COUNTRIES}
        self.assertEqual(payload["public_key"], self.api.identity["public_key"])
        self.assertEqual(payload["installation_uuid"], self.api.identity["installation_uuid"])
        # The intent must already be durable before provider-side mutation.
        self.assertEqual(failover.control_state(self.api.state)["phase"], "provisioning")
        return {"country": payload["server_country_code"]}

    def test_reconnect_first_then_country_change_without_new_identity(self):
        original_identity = self.api.state["data"]["identity.json"]
        with patch.object(failover, "request", side_effect=self.provider) as request, patch.object(failover, "native_configuration", side_effect=lambda response, identity: ("CONFIG-" + response["country"], None)):
            self.assertFalse(self.worker.step(self.api.health(attempts=1), 1000))
            request.assert_not_called()
            self.assertTrue(self.worker.step(self.api.health(attempts=2), 1000))
            control = failover.control_state(self.api.state)
            self.assertEqual(control["country"], "de")
            self.assertEqual(failover.decode_field(self.api.state, "primary.conf"), "CONFIG-de")
            self.assertFalse(self.worker.step(self.api.health(now=1010, attempts=20), 1010))
            self.assertEqual(request.call_count, 2)
            self.assertTrue(self.worker.step(self.api.health(now=1400, attempts=2), 1400))
            self.assertEqual(failover.control_state(self.api.state)["country"], "nl")
        self.assertEqual(self.api.state["data"]["identity.json"], original_identity)
        self.assertEqual([e[0] for e in self.events], ["country_change_requested", "country_configuration_applied"] * 2)

    def test_recovery_stale_health_and_pending_projection_do_not_switch(self):
        with patch.object(failover, "request") as request:
            for health in (self.api.health(ready=True), self.api.health(now=900), None,
                           {**self.api.health(), "config_id": "OTHER"},
                           {**self.api.health(), "generation": "NOT_YET_PROJECTED"}):
                self.assertFalse(self.worker.step(health, 1000))
            request.assert_not_called()
        self.assertFalse(self.api.operations)

    def test_provider_failure_retries_same_durable_intent_after_cooldown(self):
        def broken(endpoint, payload):
            if endpoint == "v1/config":
                raise TimeoutError()
            return self.provider(endpoint, payload)
        with patch.object(failover, "request", side_effect=broken):
            self.assertFalse(self.worker.step(self.api.health(), 1000))
        pending = failover.control_state(self.api.state)
        self.assertEqual(pending["phase"], "provisioning")
        self.assertEqual(failover.decode_field(self.api.state, "primary.conf"), "ORIGINAL")
        resumed = failover.Watchdog(self.api, emit=lambda *args, **kwargs: None)
        with patch.object(failover, "request", side_effect=self.provider) as request, patch.object(failover, "native_configuration", return_value=("NEW", None)):
            self.assertFalse(resumed.step(self.api.health(now=1020), 1020))
            request.assert_not_called()
            self.assertTrue(resumed.step(None, 1070))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[1]["server_country_code"], pending["target_country"])
        self.assertEqual(failover.control_state(self.api.state)["generation"], pending["operation_id"])

    def test_concurrent_manual_configuration_is_not_overwritten(self):
        def concurrent(endpoint, payload):
            response = self.provider(endpoint, payload)
            if endpoint == "v1/config":
                self.api.patch("aof-agent-vpn-state", {"primary.conf": b64(b"MANUAL")}, self.api.state["metadata"]["resourceVersion"])
            return response
        with patch.object(failover, "request", side_effect=concurrent), patch.object(failover, "native_configuration", return_value=("NEW", None)):
            self.assertFalse(self.worker.step(self.api.health(), 1000))
        self.assertEqual(failover.decode_field(self.api.state, "primary.conf"), "MANUAL")
        with patch.object(failover, "request") as request:
            self.assertFalse(self.worker.step(None, 1070))
            request.assert_not_called()
        self.assertEqual(failover.control_state(self.api.state)["phase"], "blocked")

    def test_country_selection_uses_only_available_awg_locations(self):
        self.assertEqual(failover.next_country(COUNTRIES, None), "de")
        self.assertEqual(failover.next_country(COUNTRIES, "de"), "nl")
        self.assertEqual(failover.next_country(COUNTRIES, "nl"), "gl")
        self.assertIsNone(failover.next_country([COUNTRIES[1]], "de"))
        self.assertIsNone(failover.next_country([{"server_country_code": "de", "available_protocols": ["vless"]}], None))


if __name__ == "__main__":
    unittest.main()

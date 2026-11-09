import base64
import json
import io
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import subscription
from test_connection_key import key


class SubscriptionTests(unittest.TestCase):
    def test_encrypted_request_and_response_match_gateway_protocol(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        expected = {"auth_data": {"api_key": "SYNTHETIC"}}
        body, aes_key, iv = subscription.encrypt_request(expected, public)
        self.assertNotIn(b"SYNTHETIC", body)
        document = json.loads(body)
        keys = json.loads(private.decrypt(base64.b64decode(document["key_payload"]), rsa_padding.PKCS1v15()))
        self.assertEqual(base64.b64decode(keys["aes_key"]), aes_key)
        self.assertEqual(len(base64.b64decode(keys["aes_iv"])), 32)
        self.assertEqual(subscription.decrypt_response(base64.b64decode(document["api_payload"]), aes_key, iv), expected)

    def test_configuration_is_bound_to_registered_identity(self):
        identity = subscription.new_identity()
        native = "[Interface]\nPrivateKey = $WIREGUARD_CLIENT_PRIVATE_KEY\nAddress = 10.0.0.2/32\n[Peer]\nPublicKey = SYNTHETIC\nEndpoint = 203.0.113.1:51820\nAllowedIPs = 0.0.0.0/0\n"
        def response(text):
            return {"config": key({"containers": [{"amnezia-awg": {"last_config": json.dumps({"config": text})}}]})}
        text, _ = subscription.native_configuration(response(native), identity)
        self.assertIn(identity["private_key"], text)
        with self.assertRaises(ValueError):
            subscription.native_configuration(response(native.replace("$WIREGUARD_CLIENT_PRIVATE_KEY", "WRONG_KEY")), identity)

    def test_gateway_created_response_is_success_and_errors_stay_redacted(self):
        aes_key, iv = b"k" * 32, b"i" * 32
        def encrypted(status):
            padder = padding.PKCS7(128).padder()
            raw = json.dumps({"http_status": status, "message": "SYNTHETIC_SECRET"}).encode()
            raw = padder.update(raw) + padder.finalize()
            cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv[:16])).encryptor()
            return cipher.update(raw) + cipher.finalize()
        with patch.object(subscription, "encrypt_request", return_value=(b"{}", aes_key, iv)):
            with patch.object(subscription.urllib.request, "urlopen", return_value=io.BytesIO(encrypted(201))):
                self.assertEqual(subscription.request("v1/config", {})["http_status"], 201)
            with patch.object(subscription.urllib.request, "urlopen", return_value=io.BytesIO(encrypted(403))):
                with self.assertRaisesRegex(ValueError, "^provider API status 403$"):
                    subscription.request("v1/config", {})

    def test_completed_provisioning_never_registers_another_device(self):
        source = {"data": {"connection-key": subscription.b64(key({"api_config": {"service_protocol": "awg", "service_type": "external-premium"}}).encode())}}
        state = {"metadata": {"labels": {"app.kubernetes.io/name": "aof-agent-vpn"}}, "data": {"primary.conf": "SYNTHETIC"}}
        with patch.object(subscription, "Secrets") as secrets, patch.object(subscription, "request") as request:
            secrets.return_value.get.side_effect = [source, state]
            subscription.provision("TEST", "TEST", "source", "state")
            request.assert_not_called()
            secrets.return_value.create.assert_not_called()
            secrets.return_value.update.assert_not_called()

    def test_unknown_state_secret_is_never_overwritten(self):
        source = {"data": {"connection-key": subscription.b64(key({"api_config": {"service_protocol": "awg", "service_type": "external-premium"}}).encode())}}
        with patch.object(subscription, "Secrets") as secrets, patch.object(subscription, "request") as request:
            secrets.return_value.get.side_effect = [source, {"metadata": {"labels": {}}}]
            with self.assertRaises(ValueError):
                subscription.provision("TEST", "TEST", "source", "state")
            request.assert_not_called()

    def test_refresh_reuses_persisted_device_identity(self):
        identity = subscription.new_identity()
        code = key({"api_config": {"service_protocol": "awg", "service_type": "external-premium", "user_country_code": "test"}, "auth_data": {"api_key": "SYNTHETIC"}})
        source = {"data": {"connection-key": subscription.b64(code.encode())}}
        state = {"metadata": {"labels": {"app.kubernetes.io/name": "aof-agent-vpn"}}, "data": {
            "primary.conf": "OLD", "identity.json": subscription.b64(json.dumps(identity).encode())}}
        with patch.object(subscription, "Secrets") as secrets, patch.object(subscription, "request") as request, patch.object(subscription, "native_configuration", return_value=("NEW", None)), patch.object(subscription, "new_identity") as new_identity:
            secrets.return_value.get.side_effect = [source, state]
            subscription.provision("TEST", "TEST", "source", "state", refresh=True)
            payload = request.call_args.args[1]
            self.assertEqual(payload["public_key"], identity["public_key"])
            self.assertEqual(payload["installation_uuid"], identity["installation_uuid"])
            new_identity.assert_not_called()
            secrets.return_value.create.assert_not_called()
            secrets.return_value.update.assert_called_once_with("state", {"primary.conf": subscription.b64(b"NEW")})


if __name__ == "__main__":
    unittest.main()

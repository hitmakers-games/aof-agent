import base64
import contextlib
import io
import json
import unittest
import zlib

from connection_key import decode_key, inspect_key, LIMIT


def key(data, compressed=True):
    payload = json.dumps(data).encode()
    if compressed:
        payload = len(payload).to_bytes(4, "big") + zlib.compress(payload)
    return "vpn://" + base64.urlsafe_b64encode(payload).decode().rstrip("=")


class KeyTests(unittest.TestCase):
    def test_compressed_and_uncompressed_native_keys(self):
        data = {"containers": [{"amnezia-awg": {"last_config": "SYNTHETIC_SECRET"}}]}
        for compressed in (True, False):
            with self.subTest(compressed=compressed):
                self.assertEqual(decode_key(key(data, compressed)), data)
                result = inspect_key(key(data, compressed))
                self.assertTrue(result["contains_connection_settings"])
                self.assertFalse(result["requires_provider_provisioning"])
                self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))

    def test_subscription_is_identified_without_disclosing_authentication(self):
        value = key({"config_version": 5, "api_config": {"api_key": "SYNTHETIC_SECRET"}})
        result = inspect_key(value)
        self.assertEqual(result["format"], "amnezia-subscription")
        self.assertTrue(result["requires_provider_provisioning"])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))

    def test_malformed_and_oversized_keys_are_rejected(self):
        payload = (LIMIT + 1).to_bytes(4, "big") + zlib.compress(b"{}")
        oversized = "vpn://" + base64.urlsafe_b64encode(payload).decode()
        for value in ("vpn://!", "not-a-key", key([]), oversized):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_key(value)

    def test_other_protocols_are_not_misidentified_as_amneziawg(self):
        self.assertEqual(inspect_key("vless://SYNTHETIC")["format"], "vless")
        self.assertEqual(inspect_key("ss://SYNTHETIC")["format"], "shadowsocks")

    def test_uncompressed_json_does_not_look_like_a_compression_header(self):
        self.assertEqual(decode_key(key({"xxx": 1}, compressed=False)), {"xxx": 1})

    def test_provider_fixed_signature_is_not_treated_as_exact_size(self):
        data = {"auth_data": {"api_key": "SYNTHETIC" * 50}, "api_config": {}}
        payload = bytes.fromhex("000000ff") + zlib.compress(json.dumps(data).encode())
        value = "vpn://" + base64.urlsafe_b64encode(payload).decode().rstrip("=")
        self.assertEqual(decode_key(value), data)


if __name__ == "__main__":
    unittest.main()

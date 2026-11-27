import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambda"))
KEY = Ed25519PrivateKey.generate()
APP_ID = "111111111111111111"
GUILD_ID = "222222222222222222"
TEST_ENV = {"DISCORD_APPLICATION_ID": APP_ID, "DISCORD_PUBLIC_KEY": KEY.public_key().public_bytes_raw().hex(),
            "ALLOWED_GUILD_IDS": GUILD_ID}
with patch.dict(os.environ, TEST_ENV):
    import app
from discord_bot.config import Config
from discord_bot.responses import RequestError
from discord_bot.verification import verify_request

app.LOGGER.propagate = False
COMMAND = {"type": 2, "id": "333333333333333333", "application_id": APP_ID,
           "guild_id": GUILD_ID, "member": {}, "data": {"type": 1, "name": "ping"}}


def event_for(payload, *, key=KEY, timestamp=None, encoded=False):
    body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
    timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
    signature = key.sign(timestamp.encode() + body).hex()
    return {"version": "2.0", "rawPath": "/interactions", "requestContext": {"http": {"method": "POST"}},
            "headers": {"x-signature-ed25519": signature, "x-signature-timestamp": timestamp},
            "body": base64.b64encode(body).decode() if encoded else body.decode(), "isBase64Encoded": encoded}


class HandlerTests(unittest.TestCase):
    def invoke(self, event, status):
        result = app.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], status, result)
        self.assertEqual(result["headers"]["content-type"], "application/json")
        return json.loads(result["body"])

    def test_ping_command_and_no_network(self):
        with patch("socket.socket", side_effect=AssertionError("network call")):
            body = self.invoke(event_for(COMMAND), 200)
        self.assertEqual(body, {"type": 4, "data": {"content": "Pong!", "flags": 64, "allowed_mentions": {"parse": []}}})

    def test_discord_ping_needs_no_guild(self):
        self.assertEqual(self.invoke(event_for({"type": 1}), 200), {"type": 1})

    def test_body_bytes_and_header_case(self):
        raw = '{ "type": 1, "note": "日本語" }'.encode()
        for encoded in (False, True):
            with self.subTest(encoded=encoded):
                event = event_for(raw, encoded=encoded)
                event["headers"] = {k.upper(): v for k, v in event["headers"].items()}
                self.invoke(event, 200)

    def test_tampering_and_wrong_key(self):
        event = event_for({"type": 1})
        event["body"] += " "
        self.invoke(event, 401)
        self.invoke(event_for({"type": 1}, key=Ed25519PrivateKey.generate()), 401)

    def test_signature_headers(self):
        for headers in (None, {}, {"x-signature-ed25519": "ab"}, {"x-signature-timestamp": "123"}):
            event = event_for({"type": 1}); event["headers"] = headers
            self.invoke(event, 401)
        for name in ("x-signature-ed25519", "x-signature-timestamp"):
            for value in (None, "", "xx", "1,2", []):
                with self.subTest(name=name, value=value):
                    event = event_for({"type": 1}); event["headers"][name] = value
                    self.invoke(event, 401)
            event = event_for({"type": 1})
            event["headers"][name.upper()] = event["headers"][name]
            self.invoke(event, 401)

    def test_timestamp_boundaries(self):
        now = 1_800_000_000
        for delta in (-301, -300, 0, 300, 301):
            event = event_for({"type": 1}, timestamp=now + delta)
            if abs(delta) <= 300:
                verify_request(event, event["body"].encode(), KEY.public_key(), now)
            else:
                with self.assertRaises(RequestError):
                    verify_request(event, event["body"].encode(), KEY.public_key(), now)

    def test_invalid_body_and_json(self):
        event = event_for({"type": 1}, encoded=True); event["body"] = "%%%"
        self.invoke(event, 400)
        event = event_for({"type": 1}); event["body"] = None
        self.invoke(event, 400)
        event = event_for({"type": 1}); event["isBase64Encoded"] = "false"
        self.invoke(event, 400)
        for raw in (b'{', b'[]', b'null', b'{"type":true}', b'{"type":1,"type":2}', b'{"type":NaN}'):
            self.invoke(event_for(raw), 400)
        self.invoke(event_for(b'\xff', encoded=True), 400)

    def test_contexts_rejected(self):
        for changes in ({"guild_id": "444444444444444444"}, {"application_id": "444444444444444444"},
                        {"guild_id": None}, {"member": None}, {"context": 1}, {"guild_id": []}):
            self.invoke(event_for(COMMAND | changes), 403)

    def test_unsupported_interactions_and_commands(self):
        for kind in (0, 3, 4, 5, 99):
            self.invoke(event_for({"type": kind}), 400)
        body = self.invoke(event_for(COMMAND | {"data": {"type": 1, "name": "old"}}), 200)
        self.assertEqual(body["data"]["flags"], 64)
        for data in (None, {}, {"type": True, "name": "ping"}, {"type": 1, "name": "ping", "options": None},
                     {"type": 1, "name": "ping", "options": [{"name": "extra"}]}, {"type": 1, "name": 1}):
            self.invoke(event_for(COMMAND | {"data": data}), 400)
        self.invoke(event_for(COMMAND | {"data": {"type": 1, "name": "ping", "options": []}}), 200)

    def test_http_routing_and_bad_events(self):
        self.invoke(None, 400)
        self.invoke({}, 400)
        event = event_for({"type": 1}); event["requestContext"] = None
        self.invoke(event, 400)
        event = event_for({"type": 1}); event["rawPath"] = "/wrong"
        self.invoke(event, 404)
        event = event_for({"type": 1}); event["requestContext"]["http"]["method"] = "GET"
        self.invoke(event, 404)

    def test_logs_never_include_tokens_or_raw_data(self):
        secret = "sensitive-token-and-profile"
        event = event_for(COMMAND | {"token": secret, "user": {"username": secret}})
        with self.assertLogs(app.LOGGER, "INFO") as logs:
            self.invoke(event, 200)
        self.assertNotIn(secret, "".join(logs.output))
        with patch.object(app, "dispatch", side_effect=RuntimeError(secret)):
            with self.assertLogs(app.LOGGER, "INFO") as logs:
                self.invoke(event, 500)
        self.assertNotIn(secret, "".join(logs.output))
        self.assertIn("RuntimeError", "".join(logs.output))

    def test_cold_start_flag(self):
        app._cold_start = True
        with self.assertLogs(app.LOGGER, "INFO") as logs:
            self.invoke(event_for({"type": 1}), 200)
            self.invoke(event_for({"type": 1}), 200)
        self.assertTrue(json.loads(logs.records[0].getMessage())["cold_start"])
        self.assertFalse(json.loads(logs.records[1].getMessage())["cold_start"])


class ConfigurationTests(unittest.TestCase):
    def test_invalid_config_and_deduplication(self):
        config = Config.from_environment(TEST_ENV | {"ALLOWED_GUILD_IDS": f"{GUILD_ID},{GUILD_ID}"})
        self.assertEqual(config.guild_ids, frozenset([GUILD_ID]))
        for name, value in (("DISCORD_APPLICATION_ID", "123"), ("DISCORD_PUBLIC_KEY", "xx"),
                            ("ALLOWED_GUILD_IDS", ""), ("ALLOWED_GUILD_IDS", f"{GUILD_ID},")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                Config.from_environment(TEST_ENV | {name: value})

    def test_import_has_no_network_calls(self):
        code = "import socket; socket.socket=lambda *a,**k:(_ for _ in ()).throw(AssertionError('network')); import app"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                env=os.environ | TEST_ENV | {"PYTHONPATH": str(ROOT / "lambda")})
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

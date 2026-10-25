"""Tests for the parts of server.py that need neither network nor the MCP SDK."""

import asyncio
import logging
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402

logging.getLogger("mail-mcp").addHandler(logging.NullHandler())  # keep test output quiet

TOKEN = "t" * 40


def call_asgi(app, scope):
    """Runs the ASGI app once and returns (messages sent, whether the inner app ran)."""
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    asyncio.run(app(scope, receive, send))
    return sent


class InnerApp:
    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})


def http_scope(authorization=None):
    headers = [(b"authorization", authorization.encode())] if authorization else []
    return {"type": "http", "headers": headers, "client": ("10.0.0.5", 1234)}


class BearerAuthTests(unittest.TestCase):
    def setUp(self):
        self.inner = InnerApp()
        self.app = server.BearerAuth(self.inner, TOKEN)

    def test_missing_token_is_rejected(self):
        sent = call_asgi(self.app, http_scope())
        self.assertEqual(sent[0]["status"], 401)
        self.assertFalse(self.inner.called)

    def test_wrong_token_is_rejected(self):
        for header in (f"Bearer {TOKEN}x", "Bearer wrong", TOKEN, f"bearer {TOKEN}"):
            with self.subTest(header):
                self.assertEqual(call_asgi(self.app, http_scope(header))[0]["status"], 401)
        self.assertFalse(self.inner.called)

    def test_correct_token_passes(self):
        sent = call_asgi(self.app, http_scope(f"Bearer {TOKEN}"))
        self.assertEqual(sent[0]["status"], 200)
        self.assertTrue(self.inner.called)

    def test_rejection_is_logged_with_client_ip(self):
        with self.assertLogs("mail-mcp", level="WARNING") as logs:
            call_asgi(self.app, http_scope("Bearer nope"))
        self.assertIn("10.0.0.5", logs.output[0])

    def test_websocket_is_refused_without_reaching_the_app(self):
        sent = call_asgi(self.app, {"type": "websocket", "headers": []})
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])
        self.assertFalse(self.inner.called)

    def test_unknown_scope_type_is_not_served(self):
        call_asgi(self.app, {"type": "something-else"})
        self.assertFalse(self.inner.called)

    def test_lifespan_reaches_the_app(self):
        call_asgi(self.app, {"type": "lifespan"})
        self.assertTrue(self.inner.called)


class StartupConfigTests(unittest.TestCase):
    def test_token_must_be_long_enough(self):
        with self.assertRaises(SystemExit):
            server.read_auth_token({"MCP_AUTH_TOKEN": "short"})
        with self.assertRaises(SystemExit):
            server.read_auth_token({})
        self.assertEqual(server.read_auth_token({"MCP_AUTH_TOKEN": f" {TOKEN} "}), TOKEN)

    def test_tls_files(self):
        with tempfile.TemporaryDirectory() as d:
            cert, key = Path(d, "c.crt"), Path(d, "c.key")
            cert.write_text("x"); key.write_text("y")
            args = Namespace(tls_cert=None, tls_key=None)
            self.assertEqual(server.read_tls_files(args, {}), ("", ""))
            env = {"TLS_CERT": str(cert), "TLS_KEY": str(key)}
            self.assertEqual(server.read_tls_files(args, env), (str(cert), str(key)))
            # arguments win over the config file
            other = Namespace(tls_cert=str(key), tls_key=str(cert))
            self.assertEqual(server.read_tls_files(other, env), (str(key), str(cert)))
            with self.assertRaises(SystemExit):  # only one of the two
                server.read_tls_files(args, {"TLS_CERT": str(cert)})
            with self.assertRaises(SystemExit):  # file does not exist
                server.read_tls_files(args, {"TLS_CERT": str(cert), "TLS_KEY": str(key) + ".x"})

    def test_http_listens_on_loopback_by_default(self):
        with mock.patch.object(sys, "argv", ["server.py", "--transport", "http"]):
            self.assertEqual(server.parse_args().host, "127.0.0.1")
        with mock.patch.object(sys, "argv", ["server.py", "--host", "0.0.0.0"]):
            self.assertEqual(server.parse_args().host, "0.0.0.0")

    def test_check_accounts_exit_code(self):
        ok = mock.Mock(check_connection=lambda: {
            "user": "u", "host": "h", "drafts_folder": "Drafts", "sender_addresses": ["a@b.de"]})
        bad = mock.Mock(check_connection=mock.Mock(side_effect=server.MailError("boom")))
        with mock.patch("builtins.print"):
            self.assertEqual(server.check_accounts({"one": ok}), 0)
            self.assertEqual(server.check_accounts({"one": ok, "two": bad}), 1)


if __name__ == "__main__":
    unittest.main()

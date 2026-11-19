"""Exercise the local owner and invited browser through separate HTTP(S) listeners."""

import hashlib
import json
import socket
import ssl
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.client import HTTPConnection, HTTPSConnection
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from localweb.jobs import Jobs
from localweb.sharing import Sharing
from server import Handler, LocalServer


class LanSharingTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            patch(
                "localweb.sharing.discover_interfaces",
                return_value=[{"address": "127.0.0.1", "prefix": 30}],
            )
        )
        self.host = self.enterContext(LocalServer(("127.0.0.1", 0), Handler))
        self.host.jobs = Jobs(root)
        self.host.instance = "test-instance"
        self.host.launch = "test-launch"
        self.host.cookie_name = "ArubaLocalTest"
        self.host.cookie = "local-cookie"
        self.host.token = "local-token"
        self.host.sharing = Sharing(self.host, root, port=0)
        self.addCleanup(self.host.sharing.close)
        service = threading.Thread(
            target=self.host.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        service.start()
        self.addCleanup(service.join, 3)
        self.addCleanup(self.host.shutdown)
        self.local_auth = {
            "Cookie": self.host.cookie_name + "=" + self.host.cookie,
            "X-Local-Token": self.host.token,
            "Origin": "http://127.0.0.1:" + str(self.host.server_port),
        }
        self.remote_auth = {}

    def request(self, path, *, remote=False, body=None, headers=None):
        http = (
            HTTPSConnection("127.0.0.1", self.port, context=self.client_tls, timeout=3)
            if remote
            else HTTPConnection("127.0.0.1", self.host.server_port, timeout=3)
        )
        with closing(http):
            http.request(
                "POST" if body is not None else "GET",
                path,
                body if body is None or isinstance(body, bytes) else json.dumps(body),
                {"Content-Type": "application/json", **(headers or {})},
            )
            response = http.getresponse()
            payload = response.read()
            if response.getheader("Content-Type", "").startswith("application/json"):
                payload = json.loads(payload)
            return response.status, response.headers, payload

    def enable(self):
        status, _, view = self.request(
            "/api/sharing/enable", body={"address": "127.0.0.1"}, headers=self.local_auth
        )
        self.assertEqual(status, 200)
        self.port = urlsplit(view["url"]).port
        self.origin = view["url"]
        # Enrol the self-signed leaf only after matching the fingerprint from the host UI.
        pem = ssl.get_server_certificate(("127.0.0.1", self.port), timeout=3)
        digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        self.assertEqual(digest, view["fingerprint"].replace(":", "").lower())
        self.client_tls = ssl.create_default_context(cadata=pem)
        self.client_tls.minimum_version = ssl.TLSVersion.TLSv1_2
        return urlsplit(view["invitation"]).path

    def sign_in(self, invite):
        status, headers, _ = self.request(invite, remote=True)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        cookies = SimpleCookie(headers["Set-Cookie"])
        (cookie,) = cookies.values()
        self.assertTrue(cookie.key.startswith("__Host-"))
        self.assertTrue(cookie["httponly"] and cookie["secure"])
        self.assertEqual(cookie["samesite"].lower(), "strict")
        self.assertEqual((cookie["path"], cookie["domain"]), ("/", ""))
        auth = {"Cookie": f"{cookie.key}={cookie.value}"}
        status, _, data = self.request("/api/bootstrap", remote=True, headers=auth)
        self.assertEqual(status, 200)
        self.assertTrue(data["lanClient"])
        self.assertNotEqual(data["token"], self.host.token)
        self.remote_auth = dict(auth, Origin=self.origin)
        self.remote_auth["X-Local-Token"] = data["token"]

    def test_only_invited_browser_can_read_port_workspace_and_run_jobs(self):
        invite = self.enable()
        for path in ("/", "/matrix", "/api/bootstrap", "/api/workspace"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, remote=True)[0], 403)
                self.assertEqual(self.request(path, remote=True, headers=self.local_auth)[0], 403)
        self.sign_in(invite)
        self.assertEqual(self.request(invite, remote=True)[0], 403)
        self.assertEqual(self.request("/api/bootstrap", headers=self.remote_auth)[0], 403)
        self.assertEqual(self.request("/", remote=True, headers=self.remote_auth)[0], 200)

        payload = {"action": "targets.apply", "body": {"targets": "192.0.2.3:2222 1-2"}}
        status, _, job = self.request(
            "/api/jobs", remote=True, headers=self.remote_auth, body=payload
        )
        self.assertEqual(status, 202)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, _, detail = self.request(
                "/api/jobs/" + job["id"], remote=True, headers=self.remote_auth
            )
            self.assertEqual(status, 200)
            if detail["state"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(detail["state"], "done")
        for remote, auth in ((True, self.remote_auth), (False, self.local_auth)):
            status, _, view = self.request("/api/workspace", remote=remote, headers=auth)
            self.assertEqual(status, 200)
            self.assertEqual(view["groups"][0]["interfaces"], ["1", "2"])

    def test_lan_boundaries_and_host_only_operations(self):
        self.sign_in(self.enable())
        # 127.0.0.4 is still on this PC but outside the selected 127.0.0.0/30.
        with (
            socket.create_connection(
                ("127.0.0.1", self.port), timeout=3, source_address=("127.0.0.4", 0)
            ) as peer,
            self.assertRaises((ssl.SSLError, ConnectionError)),
        ):
            self.client_tls.wrap_socket(peer, server_hostname="127.0.0.1")
        for changed in (
            {"Host": "localhost:" + str(self.port)},
            {"Origin": "http://127.0.0.1:" + str(self.port)},
            {"Origin": "https://example.invalid"},
            {"X-Local-Token": self.host.token},
        ):
            status, _, _ = self.request(
                "/api/jobs",
                remote=True,
                headers={**self.remote_auth, **changed},
                body=b"",
            )
            self.assertEqual(status, 403)
        self.assertEqual(
            self.request("/api/sharing", remote=True, headers=self.remote_auth)[0], 404
        )
        for path in (
            "/api/sharing/renew",
            "/api/sharing/enable",
            "/api/sharing/stop",
            "/api/open-folder",
            "/api/shutdown",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.request(path, remote=True, headers=self.remote_auth, body={})[0], 403
                )
        for action in (
            "backup.pick-directory",
            "history.open-folder",
            "backup.run",
            "backup.history",
        ):
            with self.subTest(action=action):
                payload = {
                    "action": action,
                    "body": {"output_directory": r"\\example.invalid\share"},
                }
                self.assertEqual(
                    self.request("/api/jobs", remote=True, headers=self.remote_auth, body=payload)[
                        0
                    ],
                    403,
                )
        self.assertFalse(self.host.jobs.closing)
        self.assertEqual(self.host.jobs.history(), [])

    def test_renew_expiry_and_reopening_invalidates_old_invites_and_sessions(self):
        first = self.enable()
        status, _, second = self.request("/api/sharing/renew", body={}, headers=self.local_auth)
        self.assertEqual(status, 200)
        self.assertEqual(self.request(first, remote=True)[0], 403)
        self.host.sharing.listener.expires = time.monotonic() - 1
        self.assertEqual(self.request(urlsplit(second["invitation"]).path, remote=True)[0], 403)
        self.assertIsNone(self.request("/api/sharing", headers=self.local_auth)[2]["invitation"])
        status, _, third = self.request("/api/sharing/renew", body={}, headers=self.local_auth)
        self.assertEqual(status, 200)
        self.sign_in(urlsplit(third["invitation"]).path)
        old_auth = self.remote_auth
        status, _, saved = self.request("/api/sharing/renew", body={}, headers=self.local_auth)
        self.assertEqual(status, 200)
        self.assertEqual(
            self.request("/api/sharing/stop", body={}, headers=self.local_auth)[2],
            {"active": False},
        )
        self.assertIsNone(self.host.sharing.listener)
        with (
            self.assertRaises(OSError),
            socket.create_connection(("127.0.0.1", self.port), timeout=1),
        ):
            self.fail("A stopped listener accepted a connection")
        fresh = self.enable()
        self.assertEqual(self.request(urlsplit(saved["invitation"]).path, remote=True)[0], 403)
        self.assertEqual(self.request("/api/bootstrap", remote=True, headers=old_auth)[0], 403)
        self.sign_in(fresh)

    def test_remote_presence_releases_host_owner_when_sharing_stops(self):
        self.sign_in(self.enable())
        with closing(
            HTTPSConnection("127.0.0.1", self.port, context=self.client_tls, timeout=3)
        ) as http:
            http.request(
                "POST",
                "/api/browser-presence",
                b"{}",
                {**self.remote_auth, "Content-Type": "application/json"},
            )
            with closing(http.getresponse()) as presence:
                self.assertEqual(presence.status, 200)
                self.assertEqual(presence.read(1), b"\n")
                self.assertEqual(len(self.host._browsers), 1)
                self.assertEqual(
                    self.request("/api/sharing/stop", body={}, headers=self.local_auth)[0], 200
                )
                self.assertEqual(presence.read(1), b"")
        deadline = time.monotonic() + 1
        while self.host._browsers and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.host._browsers)
        self.assertIsNotNone(self.host._close_after)

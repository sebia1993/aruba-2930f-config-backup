"""Exercise the owner switch and direct browser access through both HTTP(S) listeners."""

import hashlib
import json
import socket
import sqlite3
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

from aruba2930f_backup.models import Credentials


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

    def request(self, path, *, remote=False, plain=False, body=None, headers=None, method=None):
        if remote:
            # Bind the client outside the selected adapter's 127.0.0.0/30 prefix.
            options = {"source_address": ("127.0.0.4", 0), "timeout": 3}
            if plain:
                http = HTTPConnection("127.0.0.1", self.port, **options)
            else:
                http = HTTPSConnection("127.0.0.1", self.port, context=self.client_tls, **options)
        else:
            http = HTTPConnection("127.0.0.1", self.host.server_port, timeout=3)
        with closing(http):
            http.request(
                method or ("POST" if body is not None else "GET"),
                path,
                body if body is None or isinstance(body, bytes) else json.dumps(body),
                {"Content-Type": "application/json", **(headers or {})},
            )
            response = http.getresponse()
            payload = response.read()
            if response.getheader("Content-Type", "").startswith("application/json"):
                payload = json.loads(payload)
            return response.status, response.headers, payload

    def test_reservation_endpoints_and_read_only_cable_preview(self):
        status, _, queue = self.request("/api/manual-queue", headers=self.local_auth)
        self.assertEqual((status, queue), (200, {"items": []}))
        with self.host.jobs.lock:
            self.host.jobs.active = "busy-monitor"
        try:
            status, _, result = self.request(
                "/api/targets/preview",
                headers=self.local_auth,
                body={"targets": "192.0.2.1:2222 17-18"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["groups"][0]["interfaces"], ["17", "18"])
            self.assertEqual(self.host.jobs.operations.inventory_view(), [])
        finally:
            with self.host.jobs.lock:
                self.host.jobs.active = None
        status, _, job = self.request(
            "/api/jobs",
            headers=self.local_auth,
            body={"action": "targets.preview", "body": {"targets": "192.0.2.1 17"}},
        )
        self.assertEqual(status, 202)
        self.assertEqual(job["state"], "running")
        for _ in range(100):
            if self.host.jobs.get(job["id"])["state"] != "running":
                break
            time.sleep(0.01)


    def test_ping_routes_require_existing_session_and_share_the_device_slot(self):
        plan = {
            "targets": "192.0.2.11",
            "destinations": [{"ip": "198.51.100.10"}],
        }
        key = "d" * 32
        runs = "/api/ping/runs"
        body = {"requestId": key, "plan": plan}
        self.assertEqual(self.request(runs)[0], 403)
        self.assertEqual(self.request(runs, body=body)[0], 403)
        self.assertEqual(self.request("/api/ping/preview", body=plan)[0], 403)
        status, _, preview = self.request(
            "/api/ping/preview", body=plan, headers=self.local_auth
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["totalRequests"], 5)
        self.assertFalse(preview["sourceVerified"])
        self.assertEqual(
            preview["commands"][0]["command"],
            "ping 198.51.100.10 repetitions 5 timeout 2 source 192.0.2.11",
        )
        self.host.jobs.operations.credentials = Credentials("fixture-reader", "fixture")
        with (
            patch.object(self.host.jobs.controls, "awaiting_confirmation", return_value=True),
            patch.object(self.host.jobs.operations, "_ping_pair") as execute,
        ):
            status, _, receipt = self.request(runs, body=body, headers=self.local_auth)
            self.assertEqual((status, receipt["id"], receipt["accepted"]), (202, key, True))
            self.assertEqual(self.request(runs, body=body, headers=self.local_auth)[0], 202)
            self.assertEqual(self.request(runs, headers=self.local_auth)[2], {"id": key})
            detail = self.request(runs + "/" + key, headers=self.local_auth)[2]
            self.assertEqual(detail["progress"]["completed"], 0)
            self.assertEqual(self.request(runs + "/" + key)[0], 403)
            bad = dict(self.local_auth, **{"X-Local-Token": "wrong"})
            cancel = runs + "/" + key + "/cancel"
            self.assertEqual(self.request(cancel, body={}, headers=bad)[0], 403)
            self.assertEqual(self.request(cancel, body={}, headers=self.local_auth)[0], 200)
            self.assertEqual(self.request(runs, headers=self.local_auth)[2], {"id": None})
            detail = self.request(runs + "/" + key, headers=self.local_auth)[2]
            self.assertEqual(detail["state"], "cancelled")
            self.assertEqual(detail["result"]["rows"][0]["status"], "not_run")
            self.assertEqual(self.request(runs, body=body, headers=self.local_auth)[0], 409)
            execute.assert_not_called()

    def test_ping_storage_errors_do_not_echo_private_details(self):
        runs = "/api/ping/runs"
        with patch.object(
            self.host.jobs, "submit_ping_receipt", side_effect=sqlite3.OperationalError("PRIVATE")
        ):
            status, _, error = self.request(runs, body={}, headers=self.local_auth)
        self.assertEqual(status, 503)
        self.assertNotIn("PRIVATE", json.dumps(error))
        with patch.object(
            self.host.jobs, "ping_status", side_effect=sqlite3.OperationalError("PRIVATE")
        ):
            status, _, error = self.request(runs + "/" + "d" * 32, headers=self.local_auth)
        self.assertEqual(status, 503)
        self.assertNotIn("PRIVATE", json.dumps(error))

    def test_manual_receipt_http_recovery_and_storage_failure(self):
        key = "a" * 32
        path = "/api/manual-receipts/" + key
        status, _, _ = self.request(path)
        self.assertEqual(status, 403)
        status, _, missing = self.request(path, headers=self.local_auth)
        self.assertEqual(status, 404)
        self.assertEqual(missing["code"], "receipt_not_found")
        body = {
            "action": "control.quick-execute",
            "body": {
                "requestId": key,
                "operation": "poe_off",
                "targets": [{"ip": "192.0.2.1", "sshPort": 22, "port": "1"}],
            },
        }
        with patch.object(
            self.host.jobs, "submit_receipt", side_effect=sqlite3.OperationalError("PRIVATE")
        ):
            status, _, error = self.request("/api/jobs", headers=self.local_auth, body=body)
        self.assertEqual(status, 503)
        self.assertEqual(error["code"], "receipt_storage_unavailable")
        self.assertNotIn("PRIVATE", json.dumps(error))
        with patch.object(
            self.host.jobs, "manual_receipt", side_effect=sqlite3.OperationalError("PRIVATE")
        ):
            status, _, error = self.request(path, headers=self.local_auth)
        self.assertEqual(status, 503)
        self.assertEqual(error["code"], "receipt_storage_unavailable")
        status, _, closed = self.request(path + "/close", headers=self.local_auth, body={})
        self.assertEqual(status, 200)
        self.assertFalse(closed["accepted"])
        self.assertEqual(closed["state"], "not_accepted")
        status, _, error = self.request("/api/jobs", headers=self.local_auth, body=body)
        self.assertEqual(status, 409)
        self.assertFalse(self.host.jobs.pending)
        self.assertIsNone(self.host.jobs.active)
        status, _, recovered = self.request(path, headers=self.local_auth)
        self.assertEqual((status, recovered), (200, closed))
        log = (self.host.jobs.directory / "receipt-diagnostics.jsonl").read_text(encoding="utf-8")
        records = [json.loads(line) for line in log.splitlines()]
        self.assertEqual([r["httpStatus"] for r in records], [404, 503, 503, 200, 409, 200])
        self.assertTrue(all(r["requestId"] == key for r in records))
        self.assertNotIn("PRIVATE", log)
        self.assertNotIn("192.0.2.1", log)

    def test_validation_error_after_acceptance_requires_receipt_lookup(self):
        key = "b" * 32
        accepted = {
            "id": key,
            "accepted": True,
            "state": "uncertain",
            "wasQueued": False,
            "historyOnly": True,
            "message": "",
        }
        with (
            patch.object(self.host.jobs, "submit_receipt", side_effect=ValueError("PRIVATE")),
            patch.object(self.host.jobs, "manual_receipt", return_value=accepted),
        ):
            status, _, error = self.request(
                "/api/jobs",
                headers=self.local_auth,
                body={
                    "action": "control.quick-execute",
                    "body": {"requestId": key, "operation": "poe_off", "targets": []},
                },
            )
            self.assertEqual(status, 503)
            self.assertEqual(error["code"], "receipt_check_required")
            self.assertNotIn("PRIVATE", json.dumps(error))
            status, _, receipt = self.request(
                "/api/manual-receipts/" + key, headers=self.local_auth
            )
            self.assertEqual((status, receipt), (200, accepted))

    def enable(self):
        status, _, view = self.request(
            "/api/sharing/enable", body={"address": "127.0.0.1"}, headers=self.local_auth
        )
        self.assertEqual(status, 200)
        endpoint = urlsplit(view["url"])
        self.assertEqual((endpoint.scheme, endpoint.hostname), ("https", "127.0.0.1"))
        self.assertIn(endpoint.path, ("", "/"))
        self.assertFalse(endpoint.query or endpoint.fragment)
        self.assertNotIn("invitation", view.keys())
        self.port = endpoint.port
        self.origin = "https://" + endpoint.netloc
        # Enrol the self-signed leaf only after matching the fingerprint from the host UI.
        pem = ssl.get_server_certificate(("127.0.0.1", self.port), timeout=3)
        digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        self.assertEqual(digest, view["fingerprint"].replace(":", "").lower())
        client = ssl.create_default_context(cadata=pem)
        client.minimum_version = ssl.TLSVersion.TLSv1_2
        self.client_tls = client

    def sign_in(self):
        status, headers, _ = self.request("/", remote=True)
        self.assertEqual(status, 200)
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

    def test_cross_subnet_client_can_open_the_address_and_read_workspace_and_run_jobs(self):
        self.enable()
        for path in (
            "/matrix",
            "/api/bootstrap",
            "/api/workspace",
            "/api/power-history",
            "/start/obsolete",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, remote=True)[0], 403)
                self.assertEqual(self.request(path, remote=True, headers=self.local_auth)[0], 403)
        self.sign_in()
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
            route = "/api/power-history?ip=192.0.2.3&ssh_port=22&interface=1%2F2&hours=1"
            status, _, history = self.request(route, remote=remote, headers=auth)
            self.assertEqual(status, 200)
            self.assertEqual(history["points"], [])
            self.assertEqual(
                self.request(route + "&hours=168", remote=remote, headers=auth)[0], 400
            )

    def test_distinct_browsers_have_isolated_cookies_and_csrf_tokens(self):
        self.enable()
        self.sign_in()
        first = self.remote_auth
        self.sign_in()
        self.assertNotEqual(first["Cookie"], self.remote_auth["Cookie"])
        self.assertNotEqual(first["X-Local-Token"], self.remote_auth["X-Local-Token"])
        mixed = {**self.remote_auth, "X-Local-Token": first["X-Local-Token"]}
        # Authentication runs before body parsing. A zero-length POST leaves no unread
        # TLS request bytes when Windows closes the rejected connection.
        self.assertEqual(self.request("/api/jobs", remote=True, body=b"", headers=mixed)[0], 403)
        for auth in (first, self.remote_auth):
            self.assertEqual(self.request("/api/bootstrap", remote=True, headers=auth)[0], 200)
            self.assertEqual(self.request("/api/jobs", remote=True, body=b"", headers=auth)[0], 400)

    def test_plain_http_only_redirects_the_root_to_the_selected_https_address(self):
        self.enable()
        for method in ("GET", "HEAD"):
            status, headers, payload = self.request("/", method=method, remote=True, plain=True)
            self.assertIn(status, (301, 302, 307, 308))
            self.assertEqual(headers["Location"], self.origin + "/")
            self.assertIsNone(headers.get("Set-Cookie"))
            self.assertEqual(payload, b"")
        for path, method, headers in (
            ("/", "GET", {"Host": "example.invalid"}),
            ("/", "POST", {}),
            ("/api/bootstrap", "GET", {}),
            ("/api/jobs", "POST", {}),
        ):
            with self.subTest(path=path, method=method, headers=headers):
                status, response, payload = self.request(
                    path, method=method, remote=True, plain=True, headers=headers
                )
                self.assertGreaterEqual(status, 400)
                self.assertIsNone(response.get("Set-Cookie"))
                self.assertIsNone(response.get("Location"))
                self.assertEqual(payload, b"")

    def test_lan_boundaries_and_host_only_operations(self):
        self.enable()
        self.sign_in()
        for changed in (
            {"Host": "localhost:" + str(self.port)},
            {"Origin": "http://127.0.0.1:" + str(self.port)},
            {"Origin": "https://example.invalid"},
            {"Sec-Fetch-Site": "cross-site"},
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

    def test_sharing_starts_off_and_reopening_the_same_address_invalidates_sessions(self):
        self.assertFalse(self.request("/api/sharing", headers=self.local_auth)[2]["active"])
        self.assertIsNone(self.host.sharing.listener)
        self.assertEqual(self.request("/api/bootstrap")[0], 403)
        self.assertEqual(self.request("/start/" + self.host.launch)[0], 303)
        self.enable()
        self.sign_in()
        old_auth = self.remote_auth
        self.host.sharing.port = self.port
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
        self.enable()
        self.assertEqual(self.request("/api/bootstrap", remote=True, headers=old_auth)[0], 403)
        self.sign_in()
        self.assertNotEqual(old_auth["Cookie"], self.remote_auth["Cookie"])

    def test_remote_presence_releases_host_owner_when_sharing_stops(self):
        self.enable()
        self.sign_in()
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

    def test_stopping_closes_pending_peers_and_allows_an_active_job_to_finish_locally(self):
        self.enable()
        self.sign_in()
        started, resume = threading.Event(), threading.Event()
        dispatch = self.host.jobs.operations.dispatch

        def hold_job(action, body, cancel):
            started.set()
            self.assertTrue(resume.wait(5))
            return dispatch(action, body, cancel)

        with (
            socket.create_connection(("127.0.0.1", self.port), timeout=2) as pending_peer,
            patch.object(self.host.jobs.operations, "dispatch", hold_job),
        ):
            try:
                payload = {"action": "targets.apply", "body": {"targets": "192.0.2.3:2222 1-2"}}
                status, _, job = self.request(
                    "/api/jobs", remote=True, body=payload, headers=self.remote_auth
                )
                self.assertEqual(status, 202)
                self.assertTrue(started.wait(2))
                self.assertEqual(
                    self.request("/api/sharing/stop", body={}, headers=self.local_auth)[0], 200
                )
                self.assertEqual(pending_peer.recv(1), b"")
                status, _, bootstrap = self.request("/api/bootstrap", headers=self.local_auth)
                self.assertEqual(status, 200)
                self.assertEqual(bootstrap["busyJob"], job["id"])
            finally:
                resume.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, _, detail = self.request("/api/jobs/" + job["id"], headers=self.local_auth)
            self.assertEqual(status, 200)
            if detail["state"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(detail["state"], "done")
        self.assertEqual(
            self.request("/api/workspace", headers=self.local_auth)[2]["groups"][0]["interfaces"],
            ["1", "2"],
        )

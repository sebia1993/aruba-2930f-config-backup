"""Serve authenticated HTTPS on one selected subnet alongside the local launcher."""

import ipaddress
import secrets
import socket
import threading
import time
from contextlib import suppress
from http.server import ThreadingHTTPServer

from .lan import discover_interfaces, tls_context

LAN_PORT = 18766
INVITE_SECONDS = 600


class LanListener(ThreadingHTTPServer):
    is_lan = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, owner, address, prefix, port, context, fingerprint):
        self.network = ipaddress.IPv4Network((address, prefix), strict=False)
        self.context = context
        self.fingerprint = fingerprint
        self.jobs = owner.jobs
        self.instance = owner.instance
        self.browser_opening = owner.browser_opening
        self.browser_connection = owner.browser_connection
        self.guard = threading.Lock()
        self.clients = set()
        self.closed = False
        self.invite = None
        self.expires = 0
        self.token = secrets.token_urlsafe(32)
        self.cookie = secrets.token_urlsafe(32)
        self.cookie_name = "__Host-ArubaLAN" + secrets.token_hex(6)
        super().__init__((address, port), owner.RequestHandlerClass)

    def verify_request(self, request, client_address):
        return ipaddress.IPv4Address(client_address[0]) in self.network

    @property
    def authority(self):
        return f"{self.server_address[0]}:{self.server_port}"

    def issue(self):
        with self.guard:
            self.invite = secrets.token_urlsafe(32)
            self.expires = time.monotonic() + INVITE_SECONDS

    def consume(self, candidate):
        with self.guard:
            if (
                self.closed
                or not self.invite
                or time.monotonic() >= self.expires
                or not secrets.compare_digest(candidate, self.invite.encode())
            ):
                return False
            self.invite = None
            return True

    def status(self):
        with self.guard:
            valid = self.invite and time.monotonic() < self.expires
            return {
                "active": True,
                "address": self.server_address[0],
                "network": str(self.network),
                "fingerprint": self.fingerprint,
                "url": "https://" + self.authority,
                "invitation": f"https://{self.authority}/start/{self.invite}" if valid else None,
            }

    def finish_request(self, request, client_address):
        # Perform the handshake in the per-client thread so a slow peer cannot block accept().
        with self.context.wrap_socket(
            request, server_side=True, do_handshake_on_connect=False
        ) as tls:
            with self.guard:
                if self.closed:
                    return
                self.clients.add(tls)
            try:
                tls.settimeout(8)
                tls.do_handshake()
                tls.settimeout(30)
                self.RequestHandlerClass(tls, client_address, self)
            finally:
                with self.guard:
                    self.clients.discard(tls)

    def stop(self):
        with self.guard:
            self.closed = True
            self.invite = None
            for client in self.clients:
                with suppress(OSError):
                    client.shutdown(socket.SHUT_RDWR)
        self.shutdown()
        self.server_close()

    def handle_error(self, request, client_address):
        pass  # Browser cancellations and untrusted TLS probes are routine.


class Sharing:
    def __init__(self, owner, directory, port=LAN_PORT):
        self.owner = owner
        self.directory = directory
        self.port = port
        self.guard = threading.Lock()
        self.listener = None

    def status(self, interfaces=False):
        with self.guard:
            result = self.listener.status() if self.listener else {"active": False}
        if interfaces:
            result["interfaces"] = discover_interfaces()
        return result

    def enable(self, address):
        with self.guard:
            if self.listener:
                raise ValueError("먼저 현재 LAN 공유를 종료하세요.")
            nic = next((i for i in discover_interfaces() if i["address"] == address), None)
            if not nic:
                raise ValueError("이 PC에 연결된 네트워크 주소를 선택하세요.")
            context, fingerprint = tls_context(self.directory, address)
            listener = LanListener(
                self.owner, address, nic["prefix"], self.port, context, fingerprint
            )
            listener.issue()
            try:
                threading.Thread(
                    target=listener.serve_forever, kwargs={"poll_interval": 0.15}, daemon=True
                ).start()
            except BaseException:
                listener.server_close()
                raise
            self.listener = listener
            return listener.status()

    def renew(self):
        with self.guard:
            if not self.listener:
                raise ValueError("LAN 공유를 먼저 시작하세요.")
            self.listener.issue()
            return self.listener.status()

    def close(self):
        with self.guard:
            if self.listener:
                self.listener.stop()
                self.listener = None

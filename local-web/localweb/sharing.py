"""Serve authenticated HTTPS on a selected interface alongside the local launcher."""

import hmac
import secrets
import socket
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .lan import discover_interfaces, tls_context

LAN_PORT = 18766


class HttpsRedirect(BaseHTTPRequestHandler):
    """Handle only a plain HTTP navigation to the address typed in a browser."""

    def do_GET(self):
        if (
            self.requestline.split()[1] == "/"
            and self.headers.get_all("Host") == [self.server.authority]
            and self.headers.get("Sec-Fetch-Mode", "navigate") == "navigate"
            and self.headers.get("Sec-Fetch-Dest", "document") == "document"
        ):
            self._reply(307, "https://" + self.server.authority + "/")
        else:
            self._reply(403)

    do_HEAD = do_GET

    def send_error(self, code, message=None, explain=None):
        self._reply(code)

    def _reply(self, code, location=None):
        self.close_connection = True
        self.send_response_only(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()


class LanListener(ThreadingHTTPServer):
    is_lan = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, owner, address, port, context, fingerprint):
        self.context = context
        self.fingerprint = fingerprint
        self.jobs = owner.jobs
        self.instance = owner.instance
        self.browser_opening = owner.browser_opening
        self.browser_connection = owner.browser_connection
        self.guard = threading.Lock()
        self.clients = set()
        self.closed = False
        self.session_key = secrets.token_bytes(32)
        super().__init__((address, port), owner.RequestHandlerClass)
        self.cookie_name = f"__Host-ArubaLAN{self.server_port}"

    @property
    def authority(self):
        return f"{self.server_address[0]}:{self.server_port}"

    def _sign(self, purpose, nonce):
        return hmac.digest(self.session_key, f"{purpose}:{nonce}".encode("ascii"), "sha256").hex()

    def new_cookie(self):
        nonce = secrets.token_hex(16)
        return nonce + "." + self._sign("cookie", nonce)

    def session_token(self, cookie):
        if len(cookie) != 97 or not cookie.isascii():
            return None
        nonce, separator, signature = cookie.partition(".")
        if len(nonce) != 32 or not separator:
            return None
        if not secrets.compare_digest(signature, self._sign("cookie", nonce)):
            return None
        return self._sign("csrf", nonce)

    def status(self):
        return {
            "active": True,
            "address": self.server_address[0],
            "fingerprint": self.fingerprint,
            "url": "https://" + self.authority,
        }

    def finish_request(self, request, client_address):
        # A browser may initially try HTTP and HTTPS on the same port. Inspect
        # the first TLS record byte without consuming it or blocking accept().
        request.settimeout(8)
        connection = request
        with self.guard:
            if self.closed:
                return
            self.clients.add(connection)
        try:
            if request.recv(1, socket.MSG_PEEK) != b"\x16":
                HttpsRedirect(request, client_address, self)
                return
            with self.guard:
                if self.closed:
                    return
                connection = self.context.wrap_socket(
                    request, server_side=True, do_handshake_on_connect=False
                )
                self.clients.remove(request)
                self.clients.add(connection)
            with connection:
                connection.do_handshake()
                connection.settimeout(30)
                self.RequestHandlerClass(connection, client_address, self)
        finally:
            with self.guard:
                self.clients.discard(connection)

    def stop(self):
        with self.guard:
            self.closed = True
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
                if self.listener.server_address[0] == address:
                    return self.listener.status()
                raise ValueError("서버 주소를 바꾸려면 먼저 네트워크 공유를 끄세요.")
            if not any(nic["address"] == address for nic in discover_interfaces()):
                raise ValueError("이 PC에 연결된 네트워크 주소를 선택하세요.")
            context, fingerprint = tls_context(self.directory, address)
            listener = LanListener(self.owner, address, self.port, context, fingerprint)
            try:
                threading.Thread(
                    target=listener.serve_forever, kwargs={"poll_interval": 0.15}, daemon=True
                ).start()
            except BaseException:
                listener.server_close()
                raise
            self.listener = listener
            return listener.status()

    def close(self):
        with self.guard:
            if self.listener:
                self.listener.stop()
                self.listener = None

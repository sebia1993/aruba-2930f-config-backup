"""Local HTTP entry point for the Aruba 2930F Windows browser application."""

import argparse
import json
import mimetypes
import msvcrt
import os
import re
import secrets
import shutil
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
from contextlib import ExitStack, closing, contextmanager, suppress
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "core"))
from localweb.jobs import MANUAL_ACTIONS, Busy, Jobs  # noqa: E402

VERSION = "1.31.1"
STATIC = {
    "/": "index.html",
    "/matrix": "matrix.html",
    "/power-matrix.js": "power-matrix.js",
    **{
        "/static/" + name: name
        for name in (
            "app.js",
            "lan.js",
            "browser-presence.js",
            "style.css",
            "embed.css",
            "embed.js",
            "port-tools.js",
            "power-matrix.js",
            "power-history.js",
            "port-tools.css",
            "port-links.css",
            "backup-tools.js",
            "backup-tools.css",
            "control-tools.js",
            "cable-tools.js",
            "ping-tools.js",
            "ping-inventory.js",
            "ping-files.js",
            "ping-long.js",
            "ping-analysis.js",
            "ping-path.js",
            "monitoring.js",
            "manual-queue.js",
        )
    },
}
KEY = r"[0-9a-f]{32}"
LIMIT = 6 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "ArubaLocal"
    sys_version = ""

    def log_message(self, *args):
        pass  # URLs and form data must not enter server logs.

    def _headers(self, code, typ, size=0, **extra):
        self.send_response(code)
        self.send_header("Content-Type", typ)
        if size is not None:
            self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'; form-action 'self'",
        )
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()

    def _json(self, value, code=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(code, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _fail(self, code, msg):
        self._json({"error": msg}, code)

    def _receipt_reply(self, key, stage, value, status, code):
        self.server.jobs.record_receipt_diagnostic(key, stage, status, code)
        try:
            self._json(value, status)
        except OSError:
            # Acceptance may already have happened. Record only the response
            # failure and let the browser reconcile the original request ID.
            self.server.jobs.record_receipt_diagnostic(key, stage, status, "response_write_failed")

    def _receipt_storage_error(self, key, stage):
        self._receipt_reply(
            key,
            stage,
            {
                "error": "작업 이력 DB를 읽거나 기록하지 못했습니다. 접수 상태를 다시 확인하세요.",
                "code": "receipt_storage_unavailable",
            },
            503,
            "receipt_storage_unavailable",
        )

    def _lookup_receipt(self, key, *, close_missing=False):
        stage = "close" if close_missing else "lookup"
        try:
            receipt = self.server.jobs.manual_receipt(key, close_missing=close_missing)
        except sqlite3.Error:
            return self._receipt_storage_error(key, stage)
        except Busy:
            return self._receipt_reply(
                key,
                stage,
                {
                    "error": "작업 식별자의 접수 기록을 확인할 수 없습니다.",
                    "code": "receipt_conflict",
                },
                409,
                "receipt_conflict",
            )
        if receipt is None:
            return self._receipt_reply(
                key,
                stage,
                {"error": "아직 접수 기록이 없습니다.", "code": "receipt_not_found"},
                404,
                "receipt_not_found",
            )
        self._receipt_reply(key, stage, receipt, 200, receipt["state"])

    def _submit_job(self, action, body):
        manual = isinstance(action, str) and action in MANUAL_ACTIONS
        key = body.get("requestId") if manual and isinstance(body, dict) else None
        if not isinstance(key, str) or re.fullmatch(KEY, key) is None:
            return self._json(self.server.jobs.submit_receipt(action, body), 202)
        try:
            receipt = self.server.jobs.submit_receipt(action, body)
        except sqlite3.Error:
            return self._receipt_storage_error(key, "submit")
        except Busy as exc:
            value = {"error": str(exc)}
            if exc.conflicts:
                value["conflicts"] = exc.conflicts
            return self._receipt_reply(key, "submit", value, 409, "conflict")
        except ValueError:
            # A validation error is definitive only if no durable receipt
            # exists. Preparation can also raise after the journal INSERT.
            try:
                existing = self.server.jobs.manual_receipt(key)
            except sqlite3.Error:
                return self._receipt_storage_error(key, "submit")
            except Busy:
                return self._receipt_check_required(key)
            if existing is not None and existing["accepted"]:
                return self._receipt_check_required(key)
            return self._receipt_reply(
                key,
                "submit",
                {"error": "요청한 작업과 입력 형식을 확인하세요."},
                400,
                "invalid_request",
            )
        except Exception:
            # An unexpected failure may follow a committed acceptance.
            return self._receipt_check_required(key)
        self._receipt_reply(key, "submit", receipt, 202, "accepted")

    def _receipt_check_required(self, key):
        self._receipt_reply(
            key,
            "submit",
            {
                "error": "접수 처리 중 오류가 발생했습니다. 기존 요청 ID를 확인하세요.",
                "code": "receipt_check_required",
            },
            503,
            "receipt_check_required",
        )

    def _browser_presence(self):
        with self.server.browser_connection(self.connection):
            self.close_connection = True
            self._headers(200, "application/octet-stream", size=None, Connection="close")
            self.wfile.write(b"\n")
            self.wfile.flush()
            # The presence stream ends when the client closes its TCP connection, even
            # if no unload event runs or the browser stops scheduling background tabs.
            with suppress(OSError):
                self.connection.settimeout(None)
                self.connection.recv(1)

    def _trusted_host(self):
        if self.server.is_lan:
            return self.headers.get_all("Host") == [self.server.authority]
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _session_token(self):
        app = self.server
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            morsel = cookie.get(app.cookie_name)
            if morsel:
                if app.is_lan:
                    return app.session_token(morsel.value)
                if secrets.compare_digest(morsel.value.encode(), app.cookie.encode()):
                    return app.token
        except CookieError, UnicodeError:
            pass
        return None

    def _file(self, path, download=False, **extra):
        if download:
            extra["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(
                path.name, safe=""
            )
        typ = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
        }.get(path.suffix, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        with path.open("rb") as stream:
            self._headers(200, typ, os.fstat(stream.fileno()).st_size, **extra)
            shutil.copyfileobj(stream, self.wfile)

    def do_GET(self):
        if not self._trusted_host():
            return self._fail(403, "이 PC의 로컬 주소로 접속하세요.")
        path = urlsplit(self.path).path
        app = self.server
        if path == "/health":
            return self._json(
                dict(service="aruba2930f-local", version=VERSION, instance=app.instance)
            )
        if not app.is_lan and path.startswith("/start/"):
            if not secrets.compare_digest(path[7:].encode(), app.launch.encode()):
                return self._fail(
                    403, "실행 링크가 만료되었습니다. Start-Local-Web.cmd를 다시 여세요."
                )
            self._headers(
                303,
                "text/plain",
                **{
                    "Location": "/",
                    "Set-Cookie": f"{app.cookie_name}={app.cookie}; HttpOnly; SameSite=Strict; Path=/",
                },
            )
            return
        session_token = self._session_token()
        extra = {}
        if not session_token:
            if app.is_lan:
                if (
                    self.path != "/"
                    or self.headers.get("Sec-Fetch-Mode", "navigate") != "navigate"
                    or self.headers.get("Sec-Fetch-Dest", "document") != "document"
                ):
                    return self._fail(403, "브라우저 주소창에서 네트워크 공유 주소를 여세요.")
                cookie = f"{app.cookie_name}={app.new_cookie()}; Secure; HttpOnly; SameSite=Strict; Path=/"
                extra["Set-Cookie"] = cookie
            elif path == "/":
                page = """<!doctype html><html lang=ko><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Aruba 2930F 로컬 접속</title><main style="max-width:42rem;margin:12vh auto;padding:2rem;font:1.15rem/1.8 system-ui"><h1>2930F 로컬 스위치 관리</h1><p>이 브라우저에서 관리 화면을 처음 열 때는 배포 폴더의 <strong>Start-Local-Web.cmd</strong>를 실행하세요. 기본 브라우저에서 이 PC의 관리 화면이 열립니다.</p></main></html>""".encode()
                self._headers(200, "text/html; charset=utf-8", len(page))
                self.wfile.write(page)
                return
            else:
                return self._fail(403, "Start-Local-Web.cmd로 관리 화면을 여세요.")
        try:
            if path in STATIC:
                if (
                    path in {"/", "/matrix"}
                    and self.headers.get("Sec-Fetch-Mode") == "navigate"
                    and self.headers.get("Sec-Fetch-Dest") == "document"
                ):
                    app.browser_opening()
                self._file(ROOT / "static" / STATIC[path], **extra)
            elif path == "/api/bootstrap":
                self._json(
                    dict(
                        token=session_token,
                        version=VERSION,
                        instance=app.instance,
                        lanClient=app.is_lan,
                        session=app.jobs.operations.session_summary(),
                        busyJob=app.jobs.resumable_job(),
                        lastCableJob=app.jobs.latest_cable_job(),
                        monitor=app.jobs.monitor_status(),
                    )
                )
            elif path == "/api/sharing" and not app.is_lan:
                self._json(app.sharing.status(interfaces=True))
            elif path == "/api/history":
                self._json({"items": app.jobs.history()})
            elif path == "/api/power-history":
                from sqlite3 import Error as DatabaseError

                from localweb.power_history import parse_history_request

                try:
                    target = parse_history_request(urlsplit(self.path).query)
                except ValueError:
                    return self._fail(
                        400, "스위치 주소, SSH 포트, 포트 번호와 조회 기간을 확인하세요."
                    )
                try:
                    self._json(app.jobs.read_power_history(*target))
                except DatabaseError:
                    self._fail(503, "전력 추이를 읽지 못했습니다. 데이터 폴더를 확인하세요.")
            elif path == "/api/workspace":
                self._json(app.jobs.workspace())
            elif path == "/api/manual-queue":
                self._json(app.jobs.manual_queue())
            elif re.fullmatch("/api/manual-receipts/" + KEY, path):
                self._lookup_receipt(path.rsplit("/", 1)[1])
            elif path == "/api/monitor":
                self._json(app.jobs.monitor_status())
            elif path == "/api/ping/runs":
                self._json(app.jobs.current_ping())
            elif path == "/api/ping/long/runs":
                self._json(app.jobs.long_ping.current())
            elif re.fullmatch("/api/ping/long/runs/" + KEY, path):
                self._json(app.jobs.long_ping.status(path.rsplit("/", 1)[1]))
            elif path == "/api/ping/routes":
                from localweb.ping_path import recent_routes

                self._json(recent_routes(app.jobs))
            elif re.fullmatch("/api/ping/analysis/export/" + KEY + r"\.(csv|xlsx)", path):
                from localweb.ping_analysis import export_history

                key, ext = path.rsplit("/", 1)[1].split(".")
                with ExitStack() as cleanup:
                    try:
                        report = cleanup.enter_context(
                            export_history(app.jobs.directory / "ping-history.sqlite", key, ext)
                        )
                    except sqlite3.Error, OSError:
                        return self._fail(
                            503, "이력을 내보내지 못했습니다. 저장 공간과 데이터 폴더를 확인하세요."
                        )
                    # After headers are sent a disconnected client needs no second response.
                    try:
                        self._file(
                            report,
                            **{"Content-Disposition": f'attachment; filename="Ping-{key}.{ext}"'},
                        )
                    except OSError:
                        self.close_connection = True
            elif re.fullmatch("/api/ping/runs/" + KEY, path):
                raw_cursor = parse_qs(urlsplit(self.path).query).get("after", ["0"])[0]
                after = int(raw_cursor) if re.fullmatch(r"[0-9]{1,9}", raw_cursor) else 0
                try:
                    self._json(app.jobs.ping_status(path.rsplit("/", 1)[1], after))
                except sqlite3.Error:
                    self._fail(503, "Ping 작업 이력을 읽지 못했습니다. 기존 요청 ID를 확인하세요.")
            elif re.fullmatch("/api/jobs/" + KEY, path):
                raw_cursor = parse_qs(urlsplit(self.path).query).get("after", ["0"])[0]
                after = int(raw_cursor) if re.fullmatch(r"[0-9]{1,9}", raw_cursor) else 0
                self._json(
                    app.jobs.get(
                        path.rsplit("/", 1)[1],
                        after,
                        response_key=self.headers.get("X-Cable-Response-Key"),
                    )
                )
            elif re.fullmatch("/download/" + KEY, path):
                self._file(app.jobs.download(path.rsplit("/", 1)[1]), download=True)
            else:
                self._fail(404, "요청한 항목을 찾을 수 없습니다.")
        except sqlite3.Error:
            self._fail(503, "측정 이력을 읽을 수 없습니다. 저장 공간을 확인하세요.")
        except KeyError, FileNotFoundError, ValueError:
            self._fail(404, "요청한 작업 또는 저장 파일을 찾을 수 없습니다.")

    def do_POST(self):
        app = self.server
        origin = self.headers.get("Origin")
        token = self.headers.get("X-Local-Token", "")
        session_token = self._session_token()
        if (
            not self._trusted_host()
            or not session_token
            or self.headers.get("Sec-Fetch-Site") == "cross-site"
            or (
                origin is not None
                and origin
                != ("https://" if app.is_lan else "http://") + self.headers.get("Host", "")
            )
            or not secrets.compare_digest(token.encode(), session_token.encode())
        ):
            return self._fail(403, "로컬 브라우저 세션을 다시 여세요.")
        if self.headers.get_content_type() != "application/json":
            return self._fail(415, "JSON 요청만 지원합니다.")
        path = urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= length <= LIMIT:
                return self._fail(413, "요청 데이터가 너무 큽니다.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("입력 형식을 확인하세요.")
            if app.is_lan:
                if path in {"/api/shutdown", "/api/open-folder"} or path.startswith("/api/sharing"):
                    return self._fail(403, "이 작업은 서버를 실행한 PC에서 사용할 수 있습니다.")
                if path == "/api/jobs":
                    action, body = data.get("action"), data.get("body")
                    if action in ("backup.pick-directory", "history.open-folder") or (
                        action in ("backup.run", "backup.history")
                        and isinstance(body, dict)
                        and body.get("output_directory")
                    ):
                        return self._fail(403, "LAN 접속에서는 서버의 기본 백업 폴더를 사용하세요.")
            if path == "/api/sharing/enable":
                try:
                    result = app.sharing.enable(data.get("address"))
                except OSError:
                    return self._fail(409, "LAN 주소와 TCP 포트 18766 사용 여부를 확인하세요.")
                self._json(result)
            elif path == "/api/sharing/stop":
                app.sharing.close()
                self._json({"active": False})
            elif path == "/api/jobs":
                self._submit_job(data.get("action"), data.get("body"))
            elif re.fullmatch("/api/manual-receipts/" + KEY + "/close", path):
                self._lookup_receipt(path.split("/")[3], close_missing=True)
            elif path == "/api/targets/preview":
                from localweb.operations import parse_target_preview

                self._json(parse_target_preview(data))
            elif path == "/api/ping/preview":
                from localweb.ping_tools import preview_plan

                self._json(preview_plan(data))
            elif path == "/api/ping/inventory/preview":
                from localweb.ping_inventory_import import InventoryInputError, preview_inventory

                try:
                    preview = preview_inventory(data)
                except InventoryInputError as exc:
                    return self._fail(400, str(exc))
                self._json(preview)
            elif path == "/api/ping/long/preview":
                self._json(app.jobs.long_ping.preview(data))
            elif path == "/api/ping/long/runs":
                self._json(app.jobs.long_ping.submit(data), 202)
            elif re.fullmatch("/api/ping/long/runs/" + KEY + "/(pause|resume|cancel)", path):
                key, action = path.split("/")[-2:]
                revision = data.get("revision")
                if (action != "cancel" or data) and (
                    set(data) != {"revision"} or type(revision) is not int or revision < 0
                ):
                    return self._fail(400, "최근 상태를 조회하고 다시 시도하세요.")
                self._json(app.jobs.long_ping.control(key, action, revision))
            elif path.startswith("/api/ping/analysis/"):
                from localweb.ping_analysis import analysis_request

                self._json(
                    analysis_request(
                        app.jobs.directory / "ping-history.sqlite",
                        path.rsplit("/", 1)[1],
                        data,
                        jobs=app.jobs,
                    )
                )
            elif path.startswith("/api/ping/profiles/"):
                from localweb.ping_files import profile_request

                self._json(profile_request(app.jobs, path.rsplit("/", 1)[1], data))
            elif re.fullmatch("/api/ping/runs/" + KEY + "/export", path):
                from localweb.ping_files import general_excel

                report = general_excel(app.jobs, path.split("/")[4], data)
                self._headers(
                    200,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    len(report),
                    **{"Content-Disposition": 'attachment; filename="ping-results.xlsx"'},
                )
                self.wfile.write(report)
            elif path == "/api/ping/runs":
                try:
                    receipt = app.jobs.submit_ping_receipt(data)
                except sqlite3.Error:
                    return self._fail(
                        503, "Ping 접수 상태를 확인하세요. 새 요청 ID로 재실행하지 마세요."
                    )
                self._json(receipt, 202)
            elif re.fullmatch("/api/ping/runs/" + KEY + "/cancel", path):
                if data:
                    raise ValueError("Unexpected cancel options.")
                try:
                    self._json(app.jobs.cancel_ping(path.split("/")[4]))
                except sqlite3.Error:
                    self._fail(503, "Ping 취소 상태를 확인하세요.")
            elif path == "/api/monitor/start":
                try:
                    status = app.jobs.start_monitor(data)
                except ValueError as exc:
                    return self._fail(400, str(exc))
                self._json(status)
            elif path == "/api/monitor/stop":
                self._json(app.jobs.stop_monitor(data.get("monitorId")))
            elif re.fullmatch("/api/jobs/" + KEY + "/cancel", path):
                self._json(
                    app.jobs.cancel(path.split("/")[3], queued_only=data.get("queuedOnly") is True)
                )
            elif path == "/api/open-folder":
                handle = data.get("fileId")
                if not isinstance(handle, str) or not re.fullmatch(KEY, handle):
                    return self._fail(400, "저장된 결과 파일을 선택하세요.")
                try:
                    folder = app.jobs.result_folder(handle)
                except KeyError, OSError, ValueError:
                    return self._fail(404, "이 작업의 결과 폴더를 찾을 수 없습니다.")
                try:
                    os.startfile(folder, "explore")
                except OSError:
                    return self._fail(500, "Windows 탐색기에서 결과 폴더를 열지 못했습니다.")
                self._json({"opened": True})
            elif path == "/api/shutdown":
                app.claim_shutdown()
                try:
                    self._json({"stopped": True})
                finally:
                    threading.Thread(target=app.shutdown, daemon=True).start()
            elif path == "/api/browser-presence":
                self._browser_presence()
            else:
                self._fail(404, "요청한 작업을 찾을 수 없습니다.")
        except Busy as exc:
            conflicts = getattr(exc, "conflicts", ())
            if conflicts:
                self._json({"error": str(exc), "conflicts": conflicts}, 409)
            else:
                self._fail(409, str(exc))
        except sqlite3.Error, OSError:
            self._fail(
                503, "로컬 데이터를 읽거나 저장하지 못했습니다. 저장 공간과 권한을 확인하세요."
            )
        except ValueError as exc:
            if (
                path.startswith(("/api/ping/profiles/", "/api/ping/long/", "/api/ping/analysis/"))
                and type(exc) is ValueError
            ):
                # Only application validation text is safe; codec/parser exceptions may reflect the input.
                return self._fail(400, "입력 값, 대상 범위와 파일 형식을 확인하세요.")
            self._fail(400, "요청한 작업과 입력 형식을 확인하세요.")
        except RecursionError:
            self._fail(400, "요청한 작업과 입력 형식을 확인하세요.")
        except KeyError:
            self._fail(404, "작업을 찾을 수 없습니다.")


class LocalServer(ThreadingHTTPServer):
    is_lan = False
    # Chrome loads the dashboard scripts, styles and embedded viewer concurrently.
    request_queue_size = 64

    daemon_threads = True
    browser_close_grace = 5.0
    browser_attach_grace = 20.0

    def __init__(self, *args, **kwargs):
        self._browser_lock = threading.Lock()
        self._browsers = set()
        self._close_after = None
        self._stopping = False
        super().__init__(*args, **kwargs)

    def browser_opening(self):
        # A top-level navigation can be closed before its JavaScript attaches.
        with self._browser_lock:
            if not self._browsers and not self._stopping:
                self._close_after = time.monotonic() + self.browser_attach_grace

    @contextmanager
    def browser_connection(self, connection):
        with self._browser_lock:
            if self._stopping:
                raise Busy("서버를 종료하고 있습니다.")
            self._browsers.add(connection)
            self._close_after = None
        try:
            yield
        finally:
            with self._browser_lock:
                self._browsers.discard(connection)
                if not self._browsers and not self._stopping:
                    self._close_after = time.monotonic() + self.browser_close_grace

    def claim_shutdown(self):
        with self._browser_lock:
            self.jobs.claim_shutdown()
            self._stopping = True

    def service_actions(self):
        with self._browser_lock:
            if self._stopping:
                return
            if self._close_after is None:
                self.jobs.tick_monitor()
                return
            if time.monotonic() < self._close_after:
                return
            self.jobs.invalidate_pending("마지막 브라우저 연결이 종료되어 예약을 취소했습니다.")
            self.jobs.stop_monitor()
            try:
                self.jobs.claim_shutdown()
            except Busy:
                # A running operation owns the device until it has saved its result.
                self._close_after = time.monotonic() + 0.5
                return
            self._stopping = True
        # BaseServer.shutdown() must be called outside the serve_forever thread.
        threading.Thread(target=self.shutdown, daemon=True).start()

    def server_close(self):
        with self._browser_lock:
            self._stopping = True
            # The socket constructor may close before the app has attached its job manager.
            if hasattr(self, "jobs"):
                with self.jobs.lock:
                    self.jobs.long_ping.stop("SERVER_SHUTDOWN")
                self.jobs.invalidate_pending("서버가 종료되어 예약을 취소했습니다.")
            for connection in self._browsers:
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
        super().server_close()

    def handle_error(self, request, client_address):
        pass  # Browser navigation may close an in-flight request.


def state(directory):
    return directory / "server-info.json"


def open_existing(directory, no_browser):
    opener = build_opener(ProxyHandler({}))
    for _ in range(60):
        try:
            info = json.loads(state(directory).read_text(encoding="utf-8"))
            port = int(info["port"])
            address = f"http://127.0.0.1:{port}"
            with closing(opener.open(address + "/health", timeout=0.6)) as response:
                health = json.load(response)
            if (
                health.get("service") == "aruba2930f-local"
                and health.get("instance") == info["instance"]
            ):
                if not no_browser:
                    webbrowser.open(address + "/start/" + info["launch"])
                return
        except OSError, KeyError, ValueError:
            pass
        time.sleep(0.15)
    raise RuntimeError("실행 중인 서버의 응답을 기다리는 중입니다. 잠시 후 다시 여세요.")


def start(directory, port, no_browser):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "instance.lock").open("a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return open_existing(directory, no_browser)
        try:
            server = LocalServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            if exc.winerror != 10048 or not port:
                raise
            server = LocalServer(("127.0.0.1", 0), Handler)
        with server:
            from localweb.sharing import Sharing

            server.jobs = Jobs(directory)
            server.sharing = Sharing(server, directory)
            server.launch, server.cookie, server.token = (
                secrets.token_urlsafe(32) for _ in range(3)
            )
            server.cookie_name = "ArubaLocal" + str(server.server_port)
            server.instance = secrets.token_hex(16)
            info = dict(
                port=server.server_port,
                instance=server.instance,
                launch=server.launch,
                version=VERSION,
            )
            tmp = state(directory).with_suffix(".tmp")
            tmp.write_text(json.dumps(info), encoding="utf-8")
            tmp.replace(state(directory))
            try:
                if not no_browser:
                    webbrowser.open(f"http://127.0.0.1:{server.server_port}/start/{server.launch}")
                server.serve_forever(poll_interval=0.2)
            finally:
                server.sharing.close()
                state(directory).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=f"Aruba2930F Local Web {VERSION}")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ["LOCALAPPDATA"]) / "Aruba2930FLocalWeb" / "data",
    )
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        if args.smoke_test:
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as temp, LocalServer(("127.0.0.1", 0), Handler) as server:
                server.jobs = Jobs(Path(temp))
                server.instance = secrets.token_hex(16)
                for asset in STATIC.values():
                    if not (ROOT / "static" / asset).is_file():
                        raise FileNotFoundError(asset)
                probe = threading.Thread(target=server.handle_request, daemon=True)
                probe.start()
                with build_opener(ProxyHandler({})).open(
                    f"http://127.0.0.1:{server.server_port}/health", timeout=5
                ) as res:
                    if json.load(res)["instance"] != server.instance:
                        raise RuntimeError("health probe")
                probe.join(5)
            print(json.dumps({"version": VERSION, "ok": True}))
        else:
            start(args.data_dir, args.port, args.no_browser)
    except Exception as exc:
        from aruba2930f_backup.diagnostics import diagnostic_code_for_exception

        diagnostic = diagnostic_code_for_exception(exc, version=VERSION)
        if sys.stderr is not None:
            print(f"Startup diagnostic: {diagnostic}", file=sys.stderr)
            raise
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0,
            f"로컬 서버를 시작할 수 없습니다. ZIP 압축 해제와 데이터 폴더의 쓰기 권한을 확인하세요.\n진단 코드 {diagnostic}",
            "Aruba 2930F",
            0x10,
        )
        sys.exit(1)

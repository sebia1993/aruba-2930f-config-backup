"""Local HTTP entry point for the Aruba 2930F Windows browser application."""

import argparse
import json
import mimetypes
import msvcrt
import os
import re
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from contextlib import closing
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "core"))
from localweb.jobs import Busy, Jobs  # noqa: E402 - import after configuring the bundled core path.

VERSION = "1.3.4"
STATIC = {
    "/": "index.html",
    "/logs": "logs.html",
    "/csv": "csv.html",
    **{
        "/static/" + name: name
        for name in (
            "app.js",
            "style.css",
            "embed.css",
            "embed.js",
            "port-tools.js",
            "port-tools.css",
            "backup-tools.js",
            "backup-tools.css",
            "control-tools.js",
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

    def _trusted_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _signed_in(self):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            morsel = cookie.get(self.server.cookie_name)
            return bool(
                morsel
                and secrets.compare_digest(morsel.value.encode(), self.server.cookie.encode())
            )
        except CookieError, UnicodeError:
            return False

    def _file(self, path, download=False):
        extra = {}
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
        if path.startswith("/start/"):
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
        if not self._signed_in():
            if path == "/":
                page = """<!doctype html><html lang=ko><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Aruba 2930F 로컬 접속</title><main style="max-width:42rem;margin:12vh auto;padding:2rem;font:1.15rem/1.8 system-ui"><h1>2930F 로컬 스위치 관리</h1><p>이 브라우저에서 관리 화면을 처음 열 때는 배포 폴더의 <strong>Start-Local-Web.cmd</strong>를 실행하세요. 기본 브라우저에서 이 PC의 관리 화면이 열립니다.</p></main></html>""".encode()
                self._headers(200, "text/html; charset=utf-8", len(page))
                self.wfile.write(page)
                return
            return self._fail(403, "Start-Local-Web.cmd로 관리 화면을 여세요.")
        try:
            if path in STATIC:
                self._file(ROOT / "static" / STATIC[path])
            elif path == "/api/bootstrap":
                self._json(
                    dict(
                        token=app.token,
                        version=VERSION,
                        session=app.jobs.operations.session_summary(),
                        busyJob=app.jobs.active,
                    )
                )
            elif path == "/api/history":
                self._json({"items": app.jobs.history()})
            elif path == "/api/workspace":
                self._json(app.jobs.workspace())
            elif re.fullmatch("/api/jobs/" + KEY, path):
                raw_cursor = parse_qs(urlsplit(self.path).query).get("after", ["0"])[0]
                after = int(raw_cursor) if re.fullmatch(r"[0-9]{1,9}", raw_cursor) else 0
                self._json(app.jobs.get(path.rsplit("/", 1)[1], after))
            elif re.fullmatch("/download/" + KEY, path):
                self._file(app.jobs.download(path.rsplit("/", 1)[1]), download=True)
            else:
                self._fail(404, "요청한 항목을 찾을 수 없습니다.")
        except KeyError, FileNotFoundError, ValueError:
            self._fail(404, "요청한 작업 또는 저장 파일을 찾을 수 없습니다.")

    def do_POST(self):
        app = self.server
        origin = self.headers.get("Origin")
        token = self.headers.get("X-Local-Token", "")
        if (
            not self._trusted_host()
            or not self._signed_in()
            or self.headers.get("Sec-Fetch-Site") == "cross-site"
            or (origin is not None and origin != "http://" + self.headers.get("Host", ""))
            or not secrets.compare_digest(token.encode(), app.token.encode())
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
            if path == "/api/jobs":
                self._json({"id": app.jobs.submit(data.get("action"), data.get("body"))}, 202)
            elif re.fullmatch("/api/jobs/" + KEY + "/cancel", path):
                self._json(app.jobs.cancel(path.split("/")[3]))
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
                app.jobs.claim_shutdown()
                self._json({"stopped": True})
                threading.Thread(target=app.shutdown, daemon=True).start()
            else:
                self._fail(404, "요청한 작업을 찾을 수 없습니다.")
        except Busy as exc:
            self._fail(409, str(exc))
        except ValueError, UnicodeError, RecursionError:
            self._fail(400, "요청한 작업과 입력 형식을 확인하세요.")
        except KeyError:
            self._fail(404, "작업을 찾을 수 없습니다.")


class LocalServer(ThreadingHTTPServer):
    # Chrome loads the dashboard scripts, styles and embedded viewer concurrently.
    request_queue_size = 64

    daemon_threads = True

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
            server.jobs = Jobs(directory)
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

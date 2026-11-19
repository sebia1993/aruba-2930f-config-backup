"""Verify a Windows local-web build against a real, loopback-only SSH fixture.

Run with Python 3.14 and Paramiko. ARUBA_WEB_ROOT can point to an extracted
release. ARUBA_WEB_PYTHON can select its bundled runtime/python.exe.
"""

from __future__ import annotations

import http.cookiejar
import io
import json
import logging
import os
import secrets
import site
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import paramiko

from loopback_server import LoopbackArubaSSHServer


def interface_status_table(ports, *, terminal_width=False):
    """AOS-S fixed-width columns, including the optional friendly port name."""
    widths = (10, 19, 8, 13, 9, 12, 8, 9)
    header = ("Port", "Name", "Status", "Config-mode", "Speed", "Type", "Tagged", "Untagged")
    records = (
        (port, name, link, "Auto", "1000FDx", "100/1000T", "No", "1") for port, name, link in ports
    )
    lines = [header, tuple("-" * (width - 1) for width in widths), *records]

    def pad(value, width):
        columns = (
            sum(2 if unicodedata.east_asian_width(c) in "FW" else 1 for c in value)
            if terminal_width
            else len(value)
        )
        return value + " " * (width - columns)

    return [
        "".join(pad(value, width) for value, width in zip(row, widths, strict=True)).rstrip()
        for row in lines
    ]


class MonitorFixture(LoopbackArubaSSHServer):
    """Extend the source release's fixture with one simulated AP-535 port."""

    poe_enabled = True
    off_watts = None

    def status_lines(self):
        return interface_status_table([("1", "", "Up" if self.poe_enabled else "Down")])

    def _respond(self, channel, command):
        prompt = self.prompt
        power_command = "no interface 1 power-over-ethernet"
        if command == "show version":
            lines = ["Aruba JL255A 2930F-24G-PoE+ Switch", "Software revision WC.16.11.0025"]
        elif command == "show modules":
            lines = ["Status and Counters - Module Information", "Chassis: Aruba 2930F JL255A"]
        elif command == "show system information":
            lines = ["Status and Counters - General System Information", "System Name : edge-lab"]
        elif command == "show interfaces brief":
            lines = [
                "Port Type | Alert Enabled Status Mode",
                # Deliberately disagree with status so an accidental fallback is observable.
                f"1 100/1000T | No Yes {'Down' if self.poe_enabled else 'Up'} Auto",
            ]
        elif command == "show interfaces status":
            lines = self.status_lines()
        elif command == "show power-over-ethernet 1":
            lines = [
                "Status and Configuration Information for port 1",
                "Power Enable : " + ("Yes" if self.poe_enabled else "No"),
            ]
            if self.poe_enabled:
                lines.extend(["PoE Port Status : Delivering", "PD Power Draw : 8.1 W"])
            elif self.off_watts is not None:
                lines.append(f"PD Power Draw : {self.off_watts} W")
        elif command == "show lldp info remote-device 1":
            lines = [
                "LLDP Remote Device Information Detail",
                " Local Port :1",
                " ChassisType :mac-address",
                " ChassisId :00:11:22:33:44:55",
                " PortType :local",
                " PortId :eth0",
                " System Name :fixture-ap",
                " System Descr :Aruba AP-535",
            ]
        elif command in ("configure terminal", power_command, "interface 1 power-over-ethernet"):
            if command != "configure terminal":
                self.poe_enabled = command != power_command
            prompt = "edge-lab(config)#"
            lines = []
        elif command == "end":
            lines = []
        else:
            return super()._respond(channel, command)
        with self._lock:
            self.commands.append(command)
        channel.sendall(("\r\n".join([command, *lines, prompt])).encode("utf-8"))
        return False


def backup_switch():
    # Separate loopback addresses let real port-22 backup tests run concurrently.
    subnet = [secrets.randbelow(254) + 1 for _ in range(3)]
    address = "127." + ".".join(map(str, subnet))
    return MonitorFixture(listen_on=(address, 22))


class Http:
    def __init__(self, port, launch):
        self.base = f"http://127.0.0.1:{port}"
        self.client = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )
        self.get_bytes("/start/" + launch)
        self.token = self.get("/api/bootstrap")["token"]

    def get_bytes(self, path):
        with self.client.open(self.base + path, timeout=15) as response:
            return response.read()

    def get(self, path):
        return json.loads(self.get_bytes(path))

    def post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Local-Token": self.token,
                "Origin": self.base,
            },
        )
        with self.client.open(request, timeout=15) as response:
            return json.load(response)

    def job(self, action, *, expect_failure=False, **body):
        job_id = self.post("/api/jobs", {"action": action, "body": body})["id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = self.get("/api/jobs/" + urllib.parse.quote(job_id, safe=""))
            if status["state"] in {"done", "failed", "cancelled"}:
                if expect_failure:
                    assert status["state"] == "failed", f"{action} unexpectedly succeeded"
                    return None
                if status["state"] != "done":
                    raise AssertionError(f"{action} {status['state']}: {status.get('error')}")
                return status["result"]
            time.sleep(0.1)
        raise TimeoutError(action)

    def download(self, entry):
        return self.get_bytes("/download/" + urllib.parse.quote(entry["id"], safe=""))


def exercise(http, switch):
    address = f"127.0.0.1:{switch.port}"
    http.job("session.open", username=switch.username, password=switch.password)
    key = http.job("hostkey.inspect", targets=address)["checks"][0]
    assert key["state"] == "unknown" and key["fingerprint"].startswith("SHA256:")
    assert not switch.auth_attempts, "Host-key inspection attempted authentication"
    http.job("hostkey.approve", endpoint=address, fingerprint="unexpected", expect_failure=True)
    backup = http.job("backup.run", targets=address, max_attempts=1, concurrency=1)
    assert backup["rows"][0]["status"] == "success", backup["rows"][0].get("message")
    assert http.job("hostkey.inspect", targets=address)["checks"][0]["state"] == "trusted"
    (pin,) = http.job("hostkey.list")["keys"]
    assert pin["endpoint"] == address and pin["fingerprint"] == key["fingerprint"]
    files = {Path(f["name"]).suffix: http.download(f) for f in backup["files"]}
    assert any(b"hostname edge-lab" in raw and b"vlan 1" in raw for raw in files.values())
    assert switch.pager_advances == 1
    with zipfile.ZipFile(io.BytesIO(files[".xlsx"])) as report:
        assert "xl/workbook.xml" in report.namelist() and report.testzip() is None
    for text in files[".jsonl"].decode("utf-8").splitlines():
        assert isinstance(json.loads(text), dict)
        assert switch.password not in text
    # Old clients must not initiate an SSH collection against this endpoint.
    before = switch.commands.copy()
    try:
        http.post("/api/jobs", {"action": "logs.query", "body": {"targets": address}})
    except urllib.error.HTTPError as exc:
        assert exc.code == 400, exc.code
    else:
        raise AssertionError("Retired AP log collection job accepted")
    assert switch.commands == before

    ports = http.job("ports.query", targets=address + " 1")
    row = ports["rows"][0]
    assert row["status"] == "ok" and row["eligibleOff"], (row["status"], row["message"])
    assert row["link_status"] == "Up" and float(row["watts"]) == 8.1
    assert not ports.get("files")
    assert row["interface"] == "1" and row["enabled"] is True
    assert [n["system_name"] for n in row["lldp_neighbors"]] == ["fixture-ap"]
    assert switch.commands.count("show interfaces status") == 1
    profile = http.job("profiles.export", targets=address + " 1")["files"][0]
    profile_text = http.download(profile).decode("utf-8")
    restored = http.job("profiles.import", text=profile_text)
    assert "127.0.0.1" in restored["targets"] and str(switch.port) in restored["targets"]

    target = {"ip": "127.0.0.1", "sshPort": switch.port, "port": "1"}
    switch.off_watts = 0
    plan = http.job("control.preview", operation="poe_off", scope="batch", targets=[target])
    assert switch.poe_enabled
    assert "링크 Up" in plan["targets"][0]["detail"], plan
    result = http.job("control.apply", planId=plan["planId"], confirmation=plan["confirmationText"])
    assert result["results"][0]["status"] == "verified", result["results"][0]["message"]
    assert result["results"][0]["after"]["link"] == "Down", result
    assert result["results"][0]["before"]["watts"] == 8.1
    (reading,) = result["powerRefresh"]["readings"]
    assert (reading["ip"], reading["sshPort"], reading["port"]) == ("127.0.0.1", switch.port, "1")
    assert reading["status"] == "measured" and reading["watts"] == 0 and reading["observed"]
    assert float(result["rows"][0]["watts"]) == 0
    assert not switch.poe_enabled
    http.job(
        "control.apply",
        planId=plan["planId"],
        confirmation=plan["confirmationText"],
        expect_failure=True,
    )
    assert switch.commands.count("no interface 1 power-over-ethernet") == 1
    plan = http.job("control.preview", operation="poe_on", scope="single", targets=[target])
    assert plan["targets"][0]["link"] == "Down", plan
    result = http.job("control.apply", planId=plan["planId"], confirmation=plan["confirmationText"])
    assert result["results"][0]["status"] == "verified" and switch.poe_enabled
    assert result["results"][0]["after"]["link"] == "Up", result
    assert result["results"][0]["before"]["watts"] == 0
    (reading,) = result["powerRefresh"]["readings"]
    assert reading["status"] == "measured" and reading["watts"] == 8.1 and reading["observed"]
    assert float(result["rows"][0]["watts"]) == 8.1
    assert switch.commands.count("interface 1 power-over-ethernet") == 1
    history = http.job("control.history")["rows"]
    assert {"poe_on", "poe_off"} <= {r["operation"] for r in history if r["status"] == "verified"}
    assert http.get("/api/history")["items"]
    # A different key on this exact listening address must stop even cached-session operations.
    auth_count, command_count = len(switch.auth_attempts), len(switch.commands)
    switch.host_key = paramiko.RSAKey.generate(2048)
    blocked = http.job("backup.run", targets=address, max_attempts=1, concurrency=1)
    assert blocked["rows"][0]["status"] == "failed"
    changed = http.job("ports.refresh", targets=address + " 1")["rows"][0]
    assert changed["error_code"] == "HOST_KEY_CHANGED"
    http.job("control.inspect", targets=[target], expect_failure=True)
    assert len(switch.auth_attempts) == auth_count and len(switch.commands) == command_count
    assert http.job("hostkey.list")["keys"] == [pin]
    assert http.job("session.close")["connected"] is False


def main():
    # The unauthenticated host-key probe intentionally tears down its SSH
    # transport. Paramiko's listener otherwise logs expected Windows resets.
    transport_logger = logging.getLogger("paramiko.transport")
    transport_logger.addHandler(logging.NullHandler())
    transport_logger.propagate = False
    root = Path(os.environ.get("ARUBA_WEB_ROOT", Path(__file__).resolve().parents[1])).resolve()
    runtime = os.environ.get("ARUBA_WEB_PYTHON")
    python = Path(runtime).resolve() if runtime else root / "runtime" / "python.exe"
    if not python.exists() and not runtime:
        python = Path(sys.executable)
    if not (root / "server.py").is_file():
        raise SystemExit("Set ARUBA_WEB_ROOT to the folder containing server.py")
    with tempfile.TemporaryDirectory() as temp, MonitorFixture() as switch:
        folder = Path(temp)
        profile = folder / "profile"
        local = profile / "AppData" / "Local"
        roaming = profile / "AppData" / "Roaming"
        local.mkdir(parents=True)
        roaming.mkdir(parents=True)
        environment = dict(
            os.environ,
            USERPROFILE=str(profile),
            LOCALAPPDATA=str(local),
            APPDATA=str(roaming),
            HOME=str(profile),
        )
        if python == Path(sys.executable):
            environment["PYTHONPATH"] = os.pathsep.join(
                filter(None, [site.getusersitepackages(), environment.get("PYTHONPATH")])
            )
        command = [
            str(python),
            "-B",
            "-X",
            "utf8",
            str(root / "server.py"),
            "--no-browser",
            "--data-dir",
            str(folder),
            "--port",
            "0",
        ]
        process = subprocess.Popen(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
        )
        try:
            deadline = time.monotonic() + 30
            info = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Server exited with code {process.returncode}")
                try:
                    info = json.loads((folder / "server-info.json").read_text(encoding="utf-8"))
                    break
                except FileNotFoundError, json.JSONDecodeError:
                    time.sleep(0.1)
            if info is None:
                raise TimeoutError("Server startup")
            http = Http(info["port"], info["launch"])
            exercise(http, switch)
            http.post("/api/shutdown", {})
            assert process.wait(timeout=15) == 0
            for log in folder.rglob("*.jsonl"):
                assert switch.password not in log.read_text(encoding="utf-8")
            print(
                f"PASS {info['version']} HTTP + real loopback SSH: trust boundary, backup, downloads, "
                "retired log action, AP-535 live query, profiles, confirmed OFF/ON, replay rejection, "
                "audit history, session close and server shutdown."
            )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


if __name__ == "__main__":
    main()

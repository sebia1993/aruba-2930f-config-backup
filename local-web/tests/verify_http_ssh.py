"""Verify a Windows local-web build against a real, loopback-only SSH fixture.

Run with Python 3.14 and Paramiko. ARUBA_WEB_ROOT can point to an extracted
release. ARUBA_WEB_PYTHON can select its bundled runtime/python.exe.
"""

from __future__ import annotations

import csv
import http.cookiejar
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from loopback_server import LoopbackArubaSSHServer


class MonitorFixture(LoopbackArubaSSHServer):
    """Extend the source release's fixture with one simulated AP-535 port."""

    poe_enabled = True

    def _respond(self, channel, command):
        prompt = self.prompt
        power_command = "no interface 1 power-over-ethernet"
        if command == "show version":
            lines = ["Aruba JL255A 2930F-24G-PoE+ Switch", "Software revision WC.16.11.0025"]
        elif command == "show modules":
            lines = ["Status and Counters - Module Information", "Chassis: Aruba 2930F JL255A"]
        elif command == "show system information":
            lines = ["Status and Counters - General System Information", "System Name : edge-lab"]
        elif command == "show logging":
            lines = [
                "Event Log",
                "I 11/18/26 09:00:00 00561 ports: port 1 Applying Power to PD.",
                "I 11/18/26 09:00:01 00076 ports: port 1 is now on-line",
                "I 11/18/26 09:01:00 00561 ports: port 2 Applying Power to PD.",
                "I 11/18/26 09:01:01 00077 ports: port 2 is now off-line",
                "I 11/18/26 09:02:00 00565 ports: port 3 PD Removed.",
            ]
        elif command == "show interfaces brief":
            lines = [
                "Port Type | Alert Enabled Status Mode",
                f"1 100/1000T | No Yes {'Up' if self.poe_enabled else 'Down'} Auto",
            ]
        elif command == "show power-over-ethernet 1":
            lines = [
                "Status and Configuration Information for port 1",
                "Power Enable : " + ("Yes" if self.poe_enabled else "No"),
            ]
            if self.poe_enabled:
                lines.extend(["PoE Port Status : Delivering", "PD Power Draw : 8.1 W"])
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
    blocked = http.job("backup.run", targets=address, max_attempts=1, concurrency=1)
    assert blocked["rows"][0]["status"] != "success" and not switch.auth_attempts
    http.job("hostkey.approve", endpoint=address, fingerprint=key["fingerprint"])
    assert http.job("hostkey.inspect", targets=address)["checks"][0]["state"] == "trusted"
    backup = http.job("backup.run", targets=address, max_attempts=1, concurrency=1)
    assert backup["rows"][0]["status"] == "success", backup["rows"][0].get("message")
    files = {Path(f["name"]).suffix: http.download(f) for f in backup["files"]}
    assert any(b"hostname edge-lab" in raw and b"vlan 1" in raw for raw in files.values())
    assert switch.pager_advances == 1
    with zipfile.ZipFile(io.BytesIO(files[".xlsx"])) as report:
        assert "xl/workbook.xml" in report.namelist() and report.testzip() is None
    for text in files[".jsonl"].decode("utf-8").splitlines():
        assert isinstance(json.loads(text), dict)
        assert switch.password not in text
    log = http.job("logs.query", targets=address)["logs"][0]
    assert log["status"] == "success" and "port 1 is now on-line" in log["text"], log["message"]

    ports = http.job("ports.query", targets=address + " 1")
    row = ports["rows"][0]
    assert row["status"] == "ok" and row["eligibleOff"], (row["status"], row["message"])
    assert row["link_status"] == "Up" and float(row["watts"]) == 8.1
    text = http.download(ports["files"][0]).decode("utf-8-sig")
    saved = next(csv.DictReader(io.StringIO(text)))
    assert saved["interface"] == "1" and "fixture-ap" in saved["ap_names"]
    profile = http.job("profiles.export", targets=address + " 1")["files"][0]
    profile_text = http.download(profile).decode("utf-8")
    restored = http.job("profiles.import", text=profile_text)
    assert "127.0.0.1" in restored["targets"] and str(switch.port) in restored["targets"]

    target = {"ip": "127.0.0.1", "sshPort": switch.port, "port": "1"}
    plan = http.job("control.preview", operation="poe_off", scope="batch", targets=[target])
    assert switch.poe_enabled
    result = http.job("control.apply", planId=plan["planId"], confirmation=plan["confirmationText"])
    assert result["results"][0]["status"] == "verified", result["results"][0]["message"]
    assert not switch.poe_enabled
    http.job(
        "control.apply",
        planId=plan["planId"],
        confirmation=plan["confirmationText"],
        expect_failure=True,
    )
    assert switch.commands.count("no interface 1 power-over-ethernet") == 1
    plan = http.job("control.preview", operation="poe_on", scope="single", targets=[target])
    result = http.job("control.apply", planId=plan["planId"], confirmation=plan["confirmationText"])
    assert result["results"][0]["status"] == "verified" and switch.poe_enabled
    assert switch.commands.count("interface 1 power-over-ethernet") == 1
    history = http.job("control.history")["rows"]
    assert {"poe_on", "poe_off"} <= {r["operation"] for r in history if r["status"] == "verified"}
    assert http.get("/api/history")["items"]
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
                "live logging, AP-535 query/CSV, profiles, confirmed OFF/ON, replay rejection, "
                "audit history, session close and server shutdown."
            )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


if __name__ == "__main__":
    main()

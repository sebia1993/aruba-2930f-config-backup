"""Verify JL320A chassis detection through local-web workflows over loopback SSH."""

import re
import secrets
import sys

from browser_cable import CableSwitch
from browser_qa import (
    click_quick_poe,
    edit_query_targets,
    enter_ssh_credentials,
    expect_power_result,
    open_port_controls,
    run_browser_case,
)
from playwright.sync_api import expect


class JL320ASwitch(CableSwitch):
    def __init__(self):
        super().__init__()
        # The normal backup form uses the IP address and the standard SSH port.
        self.ip = "127." + ".".join(str(secrets.randbelow(254) + 1) for _ in range(3))
        self.port = 22
        self.stacked = True
        self.standby_model = "Aruba JL320A 2930M-24G-PoE+ Switch"
        self.draws = {"1/1": 8}
        self.ap_names = {"1/1": "matrix-ap"}

    def _respond(self, channel, command):
        prompt = self.prompt
        if command == "show version":
            lines = [
                "Image stamp:",
                "                WC.16.10.0009",
                "Boot Image:     Primary",
                "Boot ROM Version:    WC.17.02.0006",
            ]
        elif command == "show modules" and self.stacked:
            lines = [
                "Status and Counters - Module Information",
                "Stack ID : 01000200-00000001",
                "Member",
                "ID  Slot Module Description                     Serial Number Status",
                "--- ---- -------------------------------------- ------------- ------",
                "1   STK  Aruba JL325A 2p Stacking Module        SN01          Up",
                "2   STK  Aruba JL325A 2p Stacking Module        SN02          Up",
            ]
        elif command == "show stacking" and self.stacked:
            lines = [
                "Stack ID : 01000200-00000001",
                "Stack Status : Active",
                "Mbr",
                "ID  Mac Address       Model                                Pri Status",
                "--- ----------------- ------------------------------------ --- ---------------",
                "*1  020000-000001     Aruba JL320A 2930M-24G-PoE+ Switch   128 Commander",
                f" 2  020000-000002     {self.standby_model:<37} 128 Standby",
            ]
        elif command == "show modules":
            lines = [
                "Status and Counters - Module Information",
                "Chassis: 2930M-24G-PoE+ JL320A",
            ]
        elif command in {
            "interface 1/1 power-over-ethernet",
            "no interface 1/1 power-over-ethernet",
        }:
            self.poe_enabled = not command.startswith("no ")
            prompt = "edge-lab(config)#"
            lines = []
        elif command == "show power-over-ethernet 1/1" and not self.poe_enabled:
            lines = [
                "Status and Configuration Information for port 1/1",
                "Power Enable : No",
                "PD Power Draw : 0 W",
            ]
        else:
            return super()._respond(channel, command)
        with self._lock:
            self.commands.append(command)
        channel.sendall("\r\n".join([command, *lines, prompt]).encode())
        return False


def exercise(page, url, switch, other, shots, backup_directory):
    touch = page.context.browser.browser_type.name == "webkit"
    if touch:
        page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    page.locator("#queryTargets").fill(f"{switch.ip} 1/1")
    page.locator("#queryForm .primary").click()
    enter_ssh_credentials(page, switch, touch=touch)
    page.wait_for_function(
        "() => ui.rows.some(row => row.status && !row.status.endsWith('pending'))"
    )
    (row,) = page.evaluate("ui.rows")
    assert row["status"] == "ok", (row["status"], row.get("error_code"), row.get("message"))
    assert row["sku"] == "JL320A" and "2930M" in row["model"], (row["sku"], row["model"])
    assert row["software_version"] == "WC.16.10.0009", row["software_version"]
    assert row["interface"] == "1/1" and row["link_status"] == "Up"
    frame = page.frame_locator("#matrixViewer")
    tile = frame.locator("#matrix [data-port-key]")
    expect(tile).to_have_count(1)
    expect(tile).to_have_attribute("data-link", "up")
    expect(tile).to_contain_text("matrix-ap")
    if shots:
        page.screenshot(path=shots / "jl320a-port.png")
    open_port_controls(page)
    for operation, enabled in (("poe_off", False), ("poe_on", True)):
        page.locator("#operation").select_option(operation)
        page.locator("#selectEligible").click()
        expect(page.locator("#selectedCount")).to_have_text("선택 1개")
        with click_quick_poe(page, operation, [(switch.ip + ":22", "1/1")], touch=touch):
            expect(page.locator("#controlResult")).to_contain_text("검증 완료", timeout=90000)
        expect_power_result(
            page,
            switch.ip,
            "0 W" if enabled else "8 W",
            "8 W" if enabled else "0 W",
            port="1/1",
            delta="+8 W" if enabled else "\N{MINUS SIGN}8 W",
        )
        assert switch.poe_enabled is enabled
        expect(tile).to_have_attribute("data-link", "up")
    assert switch.commands.count("no interface 1/1 power-over-ethernet") == 1
    assert switch.commands.count("interface 1/1 power-over-ethernet") == 1

    page.locator('nav a[href="#cable"]').click()
    page.locator("#cableTest").click()
    page.locator("#cableConfirm").click()
    cable = page.locator("#cableResult")
    expect(cable).to_contain_text(re.compile(r"17\s*m"), timeout=90000)
    for pair in ("1-2", "3-6", "4-5", "7-8"):
        expect(cable).to_contain_text(pair)
    assert [cmd for cmd in switch.commands if "cable-diagnostics" in cmd] == [
        "clear cable-diagnostics",
        "test cable-diagnostics 1/1",
        "show cable-diagnostics 1/1",
    ]
    page.locator('nav a[href="#backup"]').click()
    page.locator("#backupDirectory").fill(str(backup_directory))
    page.locator("#backupTargets").fill(switch.ip)
    page.locator("#backupForm .primary").click()
    expect(page.locator("#backupResult")).to_contain_text("성공", timeout=90000)
    assert list(backup_directory.rglob("result.xlsx"))
    assert any(
        b"hostname edge-lab" in content and b"vlan 1" in content
        for content in (path.read_bytes() for path in backup_directory.rglob("*.txt"))
    )
    assert switch.commands.count("show running-config") == 1 and switch.pager_advances == 1
    probes = switch.commands.count("show stacking")
    assert probes >= 1, switch.commands

    # A separate, ordinary multi-switch query accepts both a standalone JL320A and a 2930F.
    switch.stacked = False
    switch.draws = {"1": 8}
    page.locator('nav a[href="#ports"]').click()
    edit_query_targets(page).fill(f"{switch.ip} 1\n{other.ip}:{other.port} 1")
    page.locator("#queryForm .primary").click()
    page.wait_for_function(
        "() => ui.rows.length === 2 && ui.rows.every(r => r.status === 'ok')", timeout=60000
    )
    assert {r["sku"] for r in page.evaluate("ui.rows")} == {"JL255A", "JL320A"}
    assert (
        switch.commands.count("show stacking") == probes and "show stacking" not in other.commands
    )

    switch.stacked = True
    switch.standby_model = "Aruba R0M68A 2930M-24SR-PoE-Class6..."
    prior_commands = len(switch.commands)
    edit_query_targets(page).fill(f"{switch.ip} 1/1")
    page.locator("#queryForm .primary").click()
    page.wait_for_function(
        "() => ui.rows.length === 1 && ui.rows[0].error_code === 'MODEL_UNSUPPORTED'",
        timeout=60000,
    )
    assert "show stacking" in switch.commands[prior_commands:]
    assert "show power-over-ethernet 1/1" not in switch.commands[prior_commands:]
    page.evaluate("() => api('/api/shutdown', {})")


if __name__ == "__main__":
    mobile = "--webkit" in sys.argv
    engine = "webkit" if mobile else "chromium"
    run_browser_case(exercise, switch_factory=JL320ASwitch, browser_engine=engine, touch=mobile)
    print(
        f"PASS {engine}: JL320A image and BootROM, stacked port, LLDP, power OFF/ON, cable TDR and backup."
    )

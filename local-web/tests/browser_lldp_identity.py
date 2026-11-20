"""Verify the displayed LLDP names and AP identity through the SSH-to-browser path."""

import re
import sys

from browser_power_matrix import PowerMatrixSwitch
from browser_qa import edit_query_targets, enter_ssh_credentials, run_browser_case
from playwright.sync_api import expect


class NeighborSwitch(PowerMatrixSwitch):
    def __init__(self):
        super().__init__()
        self.draws = {str(port): 8 for port in range(6, 15)}
        self.draws["12"] = 0
        self.neighbors = {
            "6": [("AP-SW-07", "ArubaOS (MODEL: 535)", "")],
            "7": [("3F-회의실", "ArubaOS", "bridge, wlan-access-point")],
            "8": [("AP-01", "ArubaOS", "bridge")],
            "9": [("AP-09", "Aruba 2930F Switch", "bridge, router")],
            "10": [("east-1", "Aruba AP-535", ""), ("east-2", "Aruba AP-535", "")],
            "11": [("", "Aruba AP-535", "")],
            "12": [("west-port", "Aruba AP-535", "")],
            "13": [],
        }

    def _respond(self, channel, command):
        if not command.startswith("show lldp info remote-device "):
            return super()._respond(channel, command)
        port = command.split()[-1]
        if port == "14":
            lines = ["Invalid input: LLDP read denied"]
        else:
            lines = ["LLDP Remote Device Information Detail"]
            for index, (name, description, enabled) in enumerate(self.neighbors[port], 1):
                lines.extend(
                    (
                        f" Local Port : {port}",
                        " ChassisType : mac-address",
                        f" ChassisId : 02:12:34:56:{int(port):02x}:{index:02x}",
                        " PortType : local",
                        " PortId : eth0",
                        f" SysName : {name}",
                        f" System Descr : {description}",
                        f" System Capabilities Enabled : {enabled}",
                    )
                )
                if port == "8":
                    lines.append(" System Capabilities Supported : bridge, wlan-access-point")
        with self._lock:
            self.commands.append(command)
        channel.sendall("\r\n".join([command, *lines, self.prompt]).encode("utf-8"))
        return False


def exercise(page, url, switch, _second, shots, _backup):
    mobile = page.context.browser.browser_type.name == "webkit"
    if mobile:
        page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    page.locator("#queryTargets").fill(f"{switch.ip}:{switch.port} 6-14")
    page.locator("#queryPorts").click()
    enter_ssh_credentials(page, switch, touch=mobile)
    frame = page.frame_locator("#matrixViewer")
    expect(frame.locator("#matrix [data-port-key]")).to_have_count(9, timeout=90000)

    def tile(port):
        return frame.locator(f'#matrix [data-port-key$="/{port}"]')

    for port, state, ap, neighbor in (
        ("6", "normal", "AP-SW-07", ""),
        ("7", "normal", "3F-회의실", ""),
        ("8", "limited", "", "AP-01"),
        ("9", "limited", "", "AP-09"),
        ("10", "limited", "", ""),
        ("11", "limited", "", ""),
        ("12", "unpowered", "west-port", ""),
        ("13", "limited", "", ""),
        ("14", "limited", "", ""),
    ):
        current = tile(port)
        expect(current).to_have_attribute("data-state", state)
        ap_caption = current.locator(".tile-ap-name")
        if ap:
            expect(ap_caption).to_be_visible()
            expect(ap_caption).to_have_text(ap)
        else:
            expect(ap_caption).to_have_count(0)
        if neighbor:
            caption = current.locator(".tile-neighbor-name")
            expect(caption).to_be_visible()
            expect(caption).to_have_text(neighbor)
            expect(current).to_have_accessible_name(re.compile(re.escape(neighbor)))

    def show_details(port):
        current = tile(port)
        if mobile:
            current.tap()
        else:
            current.click()
        facts = frame.locator("#detail .port-facts")
        if not facts.evaluate("element => element.open"):
            facts.get_by_text("포트 상세 정보", exact=True).click()
        expect(facts).to_be_visible()
        return facts

    def field(facts, label):
        return facts.get_by_text(label, exact=True).locator("..").locator(".data")

    facts = show_details("8")
    expect(field(facts, "LLDP 수집")).to_have_text("완료")
    expect(field(facts, "LLDP 이웃 이름")).to_have_text("AP-01")
    expect(field(facts, "LLDP AP 이름")).not_to_contain_text("AP-01")
    expect(frame.locator("#detail")).to_contain_text("AP 유형 미확인")
    if shots:
        page.screenshot(path=shots / "lldp-neighbor.png")
    facts = show_details("10")
    for name in ("east-1", "east-2"):
        expect(field(facts, "LLDP 이웃 이름")).to_contain_text(name)
        expect(field(facts, "LLDP AP 이름")).not_to_contain_text(name)
    facts = show_details("7")
    expect(field(facts, "LLDP AP 이름")).to_have_text("3F-회의실")
    facts = show_details("14")
    expect(field(facts, "LLDP 수집")).to_have_text("실패")

    page.locator("#listView").click()
    rows = frame.locator("#tbody tr[data-port-key]")
    expect(rows).to_have_count(9)
    expect(frame.get_by_role("columnheader", name="LLDP 이름")).to_be_visible()
    for port, name in (("6", "AP-SW-07"), ("7", "3F-회의실"), ("8", "AP-01"), ("9", "AP-09")):
        expect(frame.locator(f'#tbody tr[data-port-key$="/{port}"] td').last).to_contain_text(name)
    assert {f"show lldp info remote-device {i}" for i in range(6, 15)} <= set(switch.commands)

    # A new reading on the same port can retain SysName while losing all AP evidence.
    switch.neighbors["6"] = [("AP-SW-07", "ArubaOS", "")]
    page.locator("#matrixView").click()
    edit_query_targets(page).fill(f"{switch.ip}:{switch.port} 6")
    page.locator("#queryPorts").click()
    expect(frame.locator("#matrix [data-port-key]")).to_have_count(1, timeout=60000)
    expect(tile("6")).to_have_attribute("data-state", "limited", timeout=60000)
    expect(tile("6").locator(".tile-ap-name")).to_have_count(0)
    expect(tile("6").locator(".tile-neighbor-name")).to_have_text("AP-SW-07")
    page.evaluate("() => api('/api/shutdown', {})")


if __name__ == "__main__":
    mobile = "--webkit" in sys.argv
    engine = "webkit" if mobile else "chromium"
    run_browser_case(exercise, switch_factory=NeighborSwitch, browser_engine=engine, touch=mobile)
    print(f"PASS {engine}: LLDP names, AP evidence, ambiguous/absent neighbors, failures and 0 W.")

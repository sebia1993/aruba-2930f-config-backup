"""Verify IP-only discovery and explicit queries through both browser layouts and loopback SSH."""

import sys

from browser_power_matrix import PowerMatrixSwitch
from browser_qa import (
    edit_query_targets,
    enter_ssh_credentials,
    expect_no_quick_power,
    open_target_editor,
    run_browser_case,
)
from playwright.sync_api import expect
from verify_http_ssh import interface_status_table


class InventorySwitch(PowerMatrixSwitch):
    def __init__(self):
        super().__init__()
        self.links = {"1/1": "Up", "1/24": "Down", "2/A1": "Up"}
        self.draws = {"1/1": 8, "1/24": 0, "2/A1": None, "2/6": 9}
        self.deny_inventory = False

    def status_lines(self):
        if self.deny_inventory:
            return ["Permission denied"]
        records = [(port, "", link) for port, link in self.links.items()]
        records.append(("Trk1", "", "Up"))
        return interface_status_table(records)

    def _respond(self, channel, command):
        if command != "show interfaces brief":
            return super()._respond(channel, command)
        lines = ["Port Type | Alert Enabled Status Mode"]
        for port in self.links:
            kind = "SFP+" if port == "2/A1" else "100/1000T"
            lines.append(f"{port} {kind} | No Yes Up Auto")
        with self._lock:
            self.commands.append(command)
        channel.sendall("\r\n".join([command, *lines, self.prompt]).encode())
        return False


def assert_matrix(page, address, links):
    frame = page.frame_locator("#matrixViewer")
    tiles = frame.locator("#matrix [data-port-key]")
    expect(tiles).to_have_count(len(links), timeout=60000)
    for port, link in links.items():
        tile = frame.locator(f'#matrix [data-port-key="{address}/{port}"]')
        expect(tile).to_have_attribute("data-link", link.lower())
    expect(page.locator("#queryPorts")).to_be_enabled()


def device_queries(commands, prefix):
    return [command.removeprefix(prefix) for command in commands if command.startswith(prefix)]


def check_ssh_context_changes(page, switch, address, other_address):
    frame = page.frame_locator("#matrixViewer")
    tiles = frame.locator("#matrix [data-port-key]")
    pending = page.locator("#matrixGroups [data-key]")
    other_tile = page.locator(f'#matrixGroups [data-key="{other_address}|1"]')
    ports_link = page.locator('nav a[href="#ports"]')
    mixed = f"{address}\n{other_address} 1"

    # Forgetting one key clears the matching discovery even when both peers share an IP.
    edit_query_targets(page).fill(f"{address}\n{other_address}")
    page.locator("#queryPorts").click()
    expect(tiles).to_have_count(len(switch.links) + 1, timeout=60000)
    page.evaluate("location.hash = '#connect'")
    page.locator("#listKeys").click()
    row = page.locator("#knownKeys tr").filter(has=page.get_by_text(address, exact=True))
    row.get_by_role("button", name="등록 삭제").click()
    page.get_by_role("button", name="이 키 삭제").click()
    expect(row).to_have_count(0)
    ports_link.click()
    expect(pending).to_have_count(1)
    expect(other_tile).to_be_visible()

    # Explicit registrations survive both reapplying the current account and closing it.
    edit_query_targets(page).fill(mixed)
    for action in ("reapply", "close"):
        page.locator("#queryPorts").click()
        expect(tiles).to_have_count(len(switch.links) + 1, timeout=60000)
        expect(page.locator("#queryPorts")).to_be_enabled()
        page.evaluate("location.hash = '#connect'")
        if action == "reapply":
            page.locator("#username").fill(switch.username)
            page.locator("#password").fill(switch.password)
            page.get_by_role("button", name="계정 적용", exact=True).click()
            expect(page.locator("#statusText")).to_contain_text("접속 계정 적용 완료")
        else:
            page.locator("#logout").click()
            expect(page.locator("#statusText")).to_contain_text("세션 해제 완료")
        ports_link.click()
        expect(pending).to_have_count(1)
        expect(other_tile).to_be_visible()
        expect(page.locator("#queryTargets")).to_have_value(mixed)


def exercise(page, url, switch, other, shots, _backup):
    mobile = page.context.browser.browser_type.name == "webkit"
    if mobile:
        page.set_viewport_size({"width": 390, "height": 844})
    address, explicit = [f"{peer.ip}:{peer.port}" for peer in (switch, other)]
    page.goto(url)
    page.locator('nav a[href="#ports"]').click()
    targets = page.locator("#queryTargets")

    # A bare IP is also a valid offline registration, with no SSH or guessed port rows.
    open_target_editor(page)
    page.locator("#editorTargets").fill(switch.ip)
    page.locator("#verifyEditor").click()
    expect(page.locator("#acceptTargets")).to_be_enabled()
    page.locator("#acceptTargets").click()
    expect(page.locator("#targetEditor")).to_be_hidden()
    assert switch.ip in targets.input_value()
    expect(page.locator(".matrix-tile")).to_have_count(0)
    assert not switch.auth_attempts

    targets.fill(address)
    page.locator("#queryPorts").click()
    enter_ssh_credentials(page, switch, touch=mobile)
    assert_matrix(page, address, switch.links)
    frame = page.frame_locator("#matrixViewer")
    expect(frame.locator("#matrix [aria-pressed=true]")).to_have_count(0)
    expect_no_quick_power(page)
    expect(frame.locator(f'[data-port-key="{address}/2/A1"]').first).to_have_attribute(
        "data-state", "unclassified"
    )
    assert set(device_queries(switch.commands, "show lldp info remote-device ")) == set(
        switch.links
    )
    assert set(device_queries(switch.commands, "show power-over-ethernet ")) == {"1/1", "1/24"}
    assert switch.commands.count("show interfaces status") == 1
    if shots:
        page.screenshot(path=shots / "discovered-ports.png")

    edit_query_targets(page)
    open_target_editor(page, "rows")
    row = page.locator("#editTargetRows tr").first
    expect(row.locator('input[data-field="ip"]')).to_have_value(switch.ip)
    expect(row.locator('input[data-field="interfaces"]')).to_have_value("")
    page.locator("#targetClose").click()

    before = len(switch.commands)
    page.reload()
    assert_matrix(page, address, switch.links)
    assert not switch.commands[before:], "Restoring the snapshot must not start another SSH query"
    port = frame.locator(f'#matrix [data-port-key="{address}/1/1"]')
    port.click()
    page.locator("#refreshPorts").click()
    expect(page.locator("#statusText")).to_contain_text("재조회 완료", timeout=60000)
    assert device_queries(switch.commands[before:], "show lldp info remote-device ") == ["1/1"]
    if port.get_attribute("aria-pressed") == "true":
        port.click()

    # Full refresh discovers the current physical inventory, including removals and additions.
    new_links = {"1/1": "Down", "2/6": "Up", "2/A1": "Up"}
    switch.links = new_links
    before = len(switch.commands)
    page.get_by_role("button", name="전체 재조회", exact=True).click()
    expect(frame.locator(f'#matrix [data-port-key="{address}/2/6"]')).to_be_visible(timeout=60000)
    assert_matrix(page, address, new_links)
    assert set(device_queries(switch.commands[before:], "show lldp info remote-device ")) == set(
        new_links
    )
    expect(frame.locator(f'#matrix [data-port-key="{address}/1/24"]')).to_have_count(0)

    # Two endpoints on one IP remain independent, and the explicit one stays a single port.
    before = len(switch.commands)
    edit_query_targets(page).fill(f"{address} 1/1\n{explicit} 1")
    page.locator("#queryPorts").click()
    tiles = frame.locator("#matrix [data-port-key]")
    expect(tiles).to_have_count(2, timeout=60000)
    expect(frame.locator(f'#matrix [data-port-key="{explicit}/1"]')).to_be_visible()
    assert device_queries(switch.commands[before:], "show lldp info remote-device ") == ["1/1"]
    assert device_queries(other.commands, "show lldp info remote-device ") == ["1"]

    # Discovery denied on one switch cannot hide the other switch or create phantom ports.
    switch.deny_inventory = True
    before = len(switch.commands)
    edit_query_targets(page).fill(f"{address}\n{explicit} 1")
    page.locator("#queryPorts").click()
    expect(tiles).to_have_count(1, timeout=60000)
    assert_matrix(page, explicit, {"1": "Up"})
    failed = page.locator("#discoveryErrors")
    expect(failed).to_be_visible()
    expect(failed).to_contain_text(address)
    expect(failed).to_contain_text("포트 목록 조회 실패")
    assert "show interfaces status" in switch.commands[before:]
    assert not device_queries(switch.commands[before:], "show lldp info remote-device ")

    # A monitoring cycle uses the same all-port scope. Follow a real one-minute cycle on PC.
    switch.deny_inventory = False
    edit_query_targets(page).fill(address)
    toggle = page.locator("#monitorToggle")
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")
    assert_matrix(page, address, new_links)
    expect(failed).to_be_hidden()
    expect(page.locator("#monitorStatus")).to_contain_text("전체 3포트")
    if not mobile:
        before = len(switch.commands)
        switch.links = {"1/1": "Down", "1/24": "Up"}
        expect(frame.locator(f'#matrix [data-port-key="{address}/1/24"]')).to_be_visible(
            timeout=130000
        )
        assert_matrix(page, address, switch.links)
        expect(page.locator("#monitorStatus")).to_contain_text("전체 2포트")
        assert set(
            device_queries(switch.commands[before:], "show lldp info remote-device ")
        ) == set(switch.links)
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "false")

    check_ssh_context_changes(page, switch, address, explicit)
    for peer in (switch, other):
        assert all(
            command.startswith("show ") or command in {"no page", "terminal width 511", "logout"}
            for command in peer.commands
        ), peer.commands
    page.evaluate("() => api('/api/shutdown', {})")


if __name__ == "__main__":
    mobile = "--webkit" in sys.argv
    run_browser_case(
        exercise,
        switch_factory=InventorySwitch,
        browser_engine="webkit" if mobile else "chromium",
        touch=mobile,
    )
    print(
        "PASS IP-only query, physical interfaces, snapshot, per-port and full refresh, explicit scope."
    )

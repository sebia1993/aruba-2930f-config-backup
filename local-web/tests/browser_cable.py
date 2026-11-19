"""Exercise shared monitoring targets and cable TDR over a loopback SSH connection."""

import re
from threading import Event

from browser_power_matrix import PowerMatrixSwitch
from browser_qa import (
    edit_query_targets,
    enter_ssh_credentials,
    open_port_controls,
    run_browser_case,
)
from browser_unified_controls import resize_workspace
from playwright.sync_api import expect


class CableSwitch(PowerMatrixSwitch):
    def __init__(self):
        super().__init__()
        self.tested = []
        self.confirmations = []
        self.confirmed = Event()
        self.release_result = Event()
        self.release_result.set()

    def _respond(self, channel, command):
        if not command.startswith(("show cable-diagnostics ", "test cable-diagnostics ")):
            return super()._respond(channel, command)
        port = command.rsplit(" ", 1)[-1]
        with self._lock:
            self.commands.append(command)
        if command.startswith("test "):
            channel.sendall(
                (
                    command + "\r\nThis command will cause a loss of link on all tested ports."
                    "\r\nContinue (y/n)? "
                ).encode()
            )
            timeout = channel.gettimeout()
            try:
                channel.settimeout(8)
                reply = channel.recv(1)
            finally:
                channel.settimeout(timeout)
            self.confirmations.append(reply)
            if reply == b"y":
                self.tested.append(port)
                self.confirmed.set()
                self.release_result.wait(12)
            channel.sendall(b"\r\n" + self.prompt.encode())
        else:
            distance = "17m" if port in self.tested else "11m"
            lines = [
                command,
                "Cable Diagnostic Status - Copper Ports",
                "       MDI    Cable       Cable Length or",
                "Port   Pair   Status      Distance to Fault",
                "----   -----  ----------- -----------------",
                f"{port:<7}1-2    Short       {distance}",
                "       3-6    OK          8m",
                "       4-5    Open        0m",
                "       7-8    OK          8m",
                self.prompt,
            ]
            channel.sendall("\r\n".join(lines).encode())
        return False


def exercise(page, url, switch, other, shots, _backup):
    endpoint = f"127.0.0.1:{switch.port}"
    other_endpoint = f"127.0.0.1:{other.port}"
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url)
    nav = page.get_by_role("navigation", name="주 메뉴")
    cable = nav.get_by_role("link", name="케이블 테스트", exact=True)
    ports = nav.locator('a[href="#ports"]')
    cable.click()
    workspace = page.locator('[data-page="cable"]')
    expect(workspace).to_be_visible()
    expect(workspace.locator('textarea,input:not([type="hidden"])')).to_have_count(0)
    show = workspace.locator("#cableShow")
    test = workspace.locator("#cableTest")
    targets = workspace.locator("#cableTargets")
    results = workspace.locator("#cableResult")
    expect(show).to_be_disabled()
    expect(test).to_be_disabled()

    def edit(text):
        workspace.locator("#cableEdit").click()
        expect(ports).to_have_attribute("aria-current", "page")
        edit_query_targets(page).fill(text)
        cable.click()

    # A single address/port entered in monitoring control options needs no re-entry either.
    ports.click()
    expect(ports).to_have_attribute("aria-current", "page")
    open_port_controls(page)
    page.locator("#controlMode").select_option("single")
    page.locator("#singleIP").fill(other_endpoint)
    page.locator("#singlePort").fill("3")
    cable.click()
    expect(targets).to_contain_text(other_endpoint)
    expect(test).to_be_enabled()

    # A raw monitoring draft can be used before a port query or SSH login.
    edit(endpoint + " 1")
    expect(targets).to_contain_text(endpoint)
    show.click()
    enter_ssh_credentials(page, switch)
    expect(results).to_contain_text(re.compile(r"11\s*m"), timeout=60000)
    expect(workspace.locator("#cableResultTitle")).to_contain_text("저장")
    assert "show cable-diagnostics 1" in switch.commands and not switch.tested
    assert not any(cmd.startswith("test cable-diagnostics") for cmd in switch.commands)
    for pair in ("1-2", "3-6", "4-5", "7-8"):
        expect(results).to_contain_text(pair)

    ports.click()
    expect(ports).to_have_attribute("aria-current", "page")
    edit_query_targets(page).fill(endpoint + " 1-2")
    page.locator("#queryPorts").click()
    matrix = page.frame_locator("#matrixViewer")
    tiles = matrix.locator("#matrix [data-port-key]")
    expect(tiles).to_have_count(2, timeout=90000)
    # A tile narrows the scope, then an explicit checkbox overrides that tile.
    tiles.last.click()
    cable.click()
    before = len(switch.commands)
    show.click()
    expect(show).to_be_enabled(timeout=60000)
    expect(results).to_contain_text(re.compile(r"11\s*m"), timeout=60000)
    assert [cmd for cmd in switch.commands[before:] if "cable-diagnostics" in cmd] == [
        "show cable-diagnostics 2"
    ]
    ports.click()
    matrix.locator(f'#tbody tr[data-port-key="{endpoint}/1"] input').check()
    cable.click()

    review = page.locator("#review")
    test.click()
    expect(review).to_be_visible()
    expect(review).to_contain_text(endpoint)
    expect(review).to_contain_text("링크")
    review.get_by_role("button", name="검토 닫기").click()
    expect(review).to_be_hidden()
    assert not any(cmd.startswith("test cable-diagnostics") for cmd in switch.commands)
    test.click()
    page.locator("#cableConfirm").click()
    expect(workspace.locator("#cableResultTitle")).to_contain_text("테스트", timeout=90000)
    expect(results).to_contain_text(re.compile(r"17\s*m"))
    expect(results).not_to_contain_text(re.compile(r"11\s*m"))
    assert switch.tested == ["1"] and switch.confirmations == [b"y"]
    assert [cmd for cmd in switch.commands if cmd.startswith("test cable-diagnostics")] == [
        "test cable-diagnostics 1"
    ]

    for width, height in ((1440, 960), (390, 844), (320, 568), (844, 390)):
        resize_workspace(page, width, height)
        expect(cable).to_have_attribute("aria-current", "page")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        expect(results).to_contain_text(re.compile(r"17\s*m"))
        for link in nav.get_by_role("link").all():
            assert link.evaluate("""link => {
                const bounds = link.getBoundingClientRect();
                const range = document.createRange();
                range.selectNodeContents(link);
                return bounds.left >= 0 && bounds.right <= innerWidth &&
                    [...range.getClientRects()].every(r => r.left >= bounds.left - 1 &&
                    r.right <= bounds.right + 1 && r.top >= bounds.top - 1 &&
                    r.bottom <= bounds.bottom + 1);
            }"""), (width, link.text_content())
        if shots:
            page.screenshot(path=shots / f"cable-results-{width}.png")
    resize_workspace(page, 1440, 960)

    # Changing just the SSH endpoint in the editor must override old selected ports,
    # even when the IP is identical. No test command is allowed on a route change.
    edit(other_endpoint + " 2")
    expect(targets).to_contain_text(other_endpoint)
    expect(targets).not_to_contain_text(endpoint)
    test.click()
    expect(review).to_contain_text(other_endpoint)
    expect(review).not_to_contain_text(endpoint)
    page.keyboard.press("Escape")
    edit("")
    expect(show).to_be_disabled()
    expect(test).to_be_disabled()
    # A new page picks up the last saved monitoring inventory with its SSH port.
    page.reload()
    expect(targets).to_contain_text(endpoint)
    before = len(switch.commands)
    show.click()
    expect(results).to_contain_text(re.compile(r"11\s*m"), timeout=60000)
    expect(results).to_contain_text(re.compile(r"17\s*m"))
    assert [cmd for cmd in switch.commands[before:] if "cable-diagnostics" in cmd] == [
        "show cable-diagnostics 1",
        "show cable-diagnostics 2",
    ]

    # Reopening the browser during an identical remeasurement resumes the original
    # job. A device returning identical stored pairs without a completion marker
    # can only prove which values it has stored.
    ports.click()
    matrix.locator(f'#matrix [data-port-key="{endpoint}/1"]').click()
    cable.click()
    switch.confirmed.clear()
    switch.release_result.clear()
    try:
        test.click()
        page.locator("#cableConfirm").click()
        assert switch.confirmed.wait(20), "Switch did not receive the approved test"
        page.reload()
        expect(page.locator("#statusText")).to_contain_text(endpoint)
    finally:
        switch.release_result.set()
    expect(workspace.locator("#cableResultTitle")).to_contain_text("재연결한 작업", timeout=60000)
    expect(results).to_contain_text("저장된 측정값")
    expect(results).not_to_contain_text("측정 완료")
    expect(results).to_contain_text(re.compile(r"17\s*m"))
    assert switch.tested == ["1", "1"] and switch.confirmations == [b"y", b"y"]
    assert not other.commands and not other.auth_attempts
    assert "configure terminal" not in switch.commands
    assert not errors, errors
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    run_browser_case(exercise, switch_factory=CableSwitch)
    print(
        "PASS cable menu: shared targets, selected port, confirmed SSH TDR, stored and fresh results"
    )

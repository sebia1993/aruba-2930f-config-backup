"""Check the unified results and transient refresh states for five switch ports."""

import sys
from threading import Event

from browser_qa import (
    enter_ssh_credentials,
    expect_hidden_selection,
    open_port_controls,
    power_filter,
    run_browser_case,
)
from playwright.sync_api import expect
from verify_http_ssh import MonitorFixture, interface_status_table


class PowerMatrixSwitch(MonitorFixture):
    def __init__(self):
        super().__init__()
        self.draws = {"1": 0, "2": 9, "3": 8, "4": 8, "5": None}
        self.ap_names = {"4": None}
        self.allow_lldp = Event()
        self.allow_lldp.set()

    def status_lines(self):
        return interface_status_table(
            (number, "Up Down", "Down" if number == "3" else "Up") for number in self.draws
        )

    def _respond(self, channel, command):
        port = command.split()[-1]
        if command == "show interfaces brief":
            lines = ["Port Type | Alert Enabled Status Mode"]
            for number in self.draws:
                link = "Up" if number == "3" else "Down"
                lines.append(f"{number} 100/1000T | No Yes {link} Auto")
        elif command.startswith("show power-over-ethernet ") and port in self.draws:
            lines = [
                f"Status and Configuration Information for port {port}",
                "Power Enable : Yes",
                "PoE Port Status : Delivering",
                "PLC Class/Type :4/2",
                "DLC Class/Type :4/2",
                "Alloc By Actual :lldp",
                "PD Requested Power Value :25.5 W",
                "PSE Allocated Power Value :" + ("10" if port == "2" else "25.5") + " W",
            ]
            if self.draws[port] is not None:
                lines.append(f"PD Power Draw : {self.draws[port]} W")
        elif command.startswith("show lldp info remote-device ") and port in self.draws:
            if port == "3":
                self.allow_lldp.wait(15)
            lines = [
                "LLDP Remote Device Information Detail",
                f" Local Port :{port}",
                " ChassisType :mac-address",
                f" ChassisId :00:11:22:33:44:{list(self.draws).index(port) + 1:02x}",
                " PortType :local",
                " PortId :eth0",
                " System Descr :Aruba AP-535",
            ]
            if name := self.ap_names.get(port, "matrix-ap-" + port):
                lines.append(f" System Name :{name}")
        else:
            return super()._respond(channel, command)
        with self._lock:
            self.commands.append(command)
        channel.sendall("\r\n".join([command, *lines, self.prompt]).encode())
        return False


def refresh_without_result(page, port, final_state, selected_tile):
    """A submitted refresh can finish without an authoritative rows array."""
    jobs = "**/api/jobs"
    poll = "**/api/jobs/power-regression-*"
    page.route(jobs, lambda route: route.fulfill(json={"id": "power-regression-" + final_state}))
    page.route(
        poll, lambda route: route.fulfill(json={"state": final_state, "error": "재조회 실패"})
    )
    try:
        selected_tile.click()
        expect(page.locator("#selectedCount")).to_have_text("선택 1개")
        open_port_controls(page)
        page.locator("#refreshPorts").click()
        tile = page.locator(f'.matrix-tile[data-key$="|{port}"]')
        expect(tile).to_have_attribute("data-phase", "stale")
        expect(tile.locator(".tile-ap-name")).to_have_count(0)
        expect(tile).to_have_attribute("data-state", "unclassified")
        expect(page.locator("#matrixViewer")).to_be_hidden()
        tile.click()
        detail = page.locator("#matrixDetails")
        expect(detail.get_by_role("checkbox")).to_be_disabled()
        expect(detail).not_to_contain_text(" W")
        expect(detail).not_to_contain_text("matrix-ap-")
        page.locator('.matrix-tile[data-key$="|1"]').click()
        expect(detail.get_by_role("checkbox")).to_be_enabled()
        detail.get_by_role("checkbox").uncheck()
    finally:
        page.unroute(jobs)
        page.unroute(poll)


def check_power_filters(page, frame, shots):
    tiles = frame.locator("#matrix [data-port-key]")
    buttons = {
        key: power_filter(frame, key) for key in ("unpowered", "limited", "normal", "unclassified")
    }
    red, amber, green, unmeasured = buttons.values()
    red_port = frame.locator('#matrix [data-state="unpowered"]')
    green_port = frame.locator('#matrix [data-state="normal"]')
    selected = page.locator("#selectedCount")
    changes = page.locator("#portChanges")
    mobile = page.context.browser.browser_type.name == "webkit"

    def activate(button):
        if mobile:
            button.tap()
        else:
            button.focus()
            button.press("Enter")

    viewport = page.viewport_size
    try:
        red_port.click()
        green_port.click()
        expect(selected).to_have_text("선택 2개")
        for width, height in ((390, 844), (320, 568)) if mobile else ((1366, 768),):
            page.set_viewport_size({"width": width, "height": height})
            for button in buttons.values():
                expect(button).to_have_attribute("aria-pressed", "false")
                expect(button).to_be_visible()
                assert button.bounding_box()["height"] >= (44 if mobile else 24)
            activate(red)
            expect(tiles).to_have_count(1)
            expect(red_port).to_have_attribute("aria-pressed", "true")
            expect_hidden_selection(page, 1)
            # Other counts still describe their groups when a color is selected.
            expect(frame.locator("#mLimited")).to_have_text("2")
            expect(frame.locator("#mNormal")).to_have_text("1")
            activate(amber)
            expect(tiles).to_have_count(3)
            assert sorted(tiles.evaluate_all("els => els.map(el => el.dataset.state)")) == [
                "limited",
                "limited",
                "unpowered",
            ]
            expect(red).to_have_attribute("aria-pressed", "true")
            expect(amber).to_have_attribute("aria-pressed", "true")
            expect(selected).to_have_text("선택 2개")
            expect_hidden_selection(page, 1)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert frame.locator("html").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            if shots:
                page.screenshot(path=shots / f"power-states-{width}.png")
            activate(changes)
            expect(tiles).to_have_count(0)
            expect_hidden_selection(page, 2)
            activate(red)
            expect(changes).to_have_attribute("aria-pressed", "true")
            expect(amber).to_have_attribute("aria-pressed", "true")
            expect(tiles).to_have_count(0)
            activate(changes)
            expect(tiles).to_have_count(2)
            expect_hidden_selection(page, 2)
            activate(amber)
            expect(tiles).to_have_count(5)
            expect_hidden_selection(page, 0)
            activate(unmeasured)
            expect(tiles).to_have_count(1)
            expect(tiles).to_have_attribute("data-state", "unclassified")
            expect_hidden_selection(page, 2)
            activate(unmeasured)
            activate(green)
            expect(tiles).to_have_count(1)
            expect(green_port).to_have_attribute("aria-pressed", "true")
            expect_hidden_selection(page, 1)
            activate(green)
            expect(red_port).to_have_attribute("aria-pressed", "true")
            expect(green_port).to_have_attribute("aria-pressed", "true")
        red_port.click()
        green_port.click()
        expect(selected).to_have_text("선택 0개")
    finally:
        page.set_viewport_size(viewport)


def exercise(page, url, switch, _second, shots, _backup):
    page.goto(url)
    page.locator('nav a[href="#ports"]').click()
    page.locator("#queryTargets").fill(f"127.0.0.1:{switch.port} 1-5")
    page.locator("#queryForm .primary").click()
    enter_ssh_credentials(page, switch)
    matrix = page.frame_locator("#matrixViewer")
    result_tiles = matrix.locator("#matrix [data-port-key]")
    expected = ["unpowered", "limited", "normal", "limited", "unclassified"]
    expect(result_tiles).to_have_count(5, timeout=90000)
    assert result_tiles.evaluate_all("tiles => tiles.map(tile => tile.dataset.state)") == expected
    for selector, value in (
        ("mUnpowered", "1"),
        ("mLimited", "2"),
        ("mNormal", "1"),
    ):
        expect(matrix.locator("#" + selector)).to_have_text(value)
    expect(matrix.locator("#mUnclassified")).to_contain_text("1개")
    changes = page.get_by_role("button", name="변화만 보기", exact=True)
    expect(changes).to_have_attribute("aria-pressed", "false")
    changes.click()
    expect(result_tiles).to_have_count(0)
    expect(matrix.locator("#matrix .empty")).to_be_visible()
    changes.click()
    expect(result_tiles).to_have_count(5)
    expect(page.get_by_role("button", name="우선 확인", exact=True)).to_have_count(0)
    before_filters = list(switch.commands)
    check_power_filters(page, matrix, shots)
    assert switch.commands == before_filters
    green = power_filter(matrix, "normal")
    green.click()
    expect(result_tiles).to_have_count(1)
    result_tiles.click()
    expect(matrix.locator("#detail")).to_contain_text("Down")
    expect(matrix.locator("#detail")).to_contain_text("matrix-ap-3")
    page.locator("#listView").click()
    expect(matrix.locator("#tbody .pillstat.normal")).to_be_visible()
    page.locator("#matrixView").click()
    expect(matrix.locator("#detail .badge.normal")).to_be_visible()
    green.click()
    expect(matrix.locator('#matrix [data-port-key$="/3"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    tiles = page.locator(".matrix-tile")
    current = page.locator('.matrix-tile[data-key$="|3"]')
    switch.draws["3"] = 0
    switch.ap_names["3"] = "renamed-ap"
    switch.allow_lldp.clear()
    try:
        open_port_controls(page)
        page.locator("#refreshPorts").click()
        expect(current).to_have_attribute("data-phase", "loading")
        expect(current).to_have_attribute("data-state", "unclassified")
        expect(current.locator(".tile-ap-name")).to_have_count(0)
        expect(tiles.nth(1).locator(".tile-ap-name")).to_have_text("matrix-ap-2")
        expect(tiles.nth(1)).to_have_attribute("data-state", "limited")
        expect(page.locator("#queryTargets")).not_to_be_editable()
        expect(page.locator("#manageTargets")).to_be_disabled()
        expect(page.locator("#matrixViewer")).to_be_hidden()
        current.click()
        detail = page.locator("#matrixDetails")
        expect(detail.get_by_role("checkbox")).to_be_disabled()
        expect(detail).not_to_contain_text("8 W")
        expect(detail).not_to_contain_text("matrix-ap-3")
        expect(detail).to_contain_text("0 W", timeout=12000)
    finally:
        switch.allow_lldp.set()
    expect(result_tiles.nth(2)).to_have_attribute("data-state", "unpowered", timeout=60000)
    expect(page.locator("#matrixViewer")).to_be_visible()
    expect(page.locator("#matrixGroups")).to_be_hidden()
    expect(result_tiles.nth(2).locator(".tile-ap-name")).to_have_text("renamed-ap")
    expected[2] = "unpowered"
    red, amber = (power_filter(matrix, state) for state in ("unpowered", "limited"))
    red.click()
    amber.click()
    expect(result_tiles).to_have_count(4)
    changes.click()
    expect(result_tiles).to_have_count(1)
    expect(result_tiles).to_have_attribute("data-changed", "true")
    expect(result_tiles).to_have_attribute("data-port-key", f"127.0.0.1:{switch.port}/3")
    result_tiles.click()
    comparison = matrix.locator("#portComparison")
    for value in ("8 W", "0 W", "matrix-ap-3", "renamed-ap"):
        expect(comparison).to_contain_text(value)
    red.click()
    expect(changes).to_have_attribute("aria-pressed", "true")
    expect(result_tiles).to_have_count(0)
    red.click()
    expect(result_tiles).to_have_count(1)
    changes.click()
    expect(result_tiles).to_have_count(4)
    amber.click()
    red.click()
    page.reload()
    expect(result_tiles).to_have_count(5)
    assert result_tiles.evaluate_all("tiles => tiles.map(tile => tile.dataset.state)") == expected
    expect(page.locator("#queryForm")).to_be_visible()
    expect(page.locator("#matrixGroups")).to_be_hidden()
    expect(page.locator("#matrixViewer")).to_be_visible()
    changes.click()
    expect(result_tiles).to_have_count(1)
    expect(result_tiles).to_have_attribute("data-changed", "true")
    result_tiles.click()
    open_port_controls(page)
    page.locator("#refreshPorts").click()
    expect(page.locator("#statusText")).to_contain_text("재조회 완료", timeout=60000)
    expect(result_tiles).to_have_count(0, timeout=60000)
    expect(matrix.locator("#matrix .empty")).to_be_visible()
    changes.click()
    # A different LLDP name is a change even when watts and the three-state
    # classification stay identical on a measured zero-watt port.
    page.locator("#listView").click()
    matrix.locator('#tbody tr[data-port-key$="/3"] input').uncheck()
    page.locator("#matrixView").click()
    matrix.locator('#matrix [data-port-key$="/1"]').click()
    switch.ap_names["1"] = "replacement-ap"
    open_port_controls(page)
    page.locator("#refreshPorts").click()
    expect(page.locator("#matrixViewer")).to_be_visible(timeout=60000)
    expect(matrix.locator('#tbody tr[data-port-key$="/1"]')).to_contain_text(
        "replacement-ap", timeout=60000
    )
    changes.click()
    expect(result_tiles).to_have_count(1)
    expect(result_tiles).to_have_attribute("data-port-key", f"127.0.0.1:{switch.port}/1")
    result_tiles.click()
    expect(comparison.locator('[data-comparison="ap"] dd')).to_have_text(
        "matrix-ap-1 → replacement-ap"
    )
    expect(comparison.locator('[data-comparison="watts"] dd')).to_contain_text("0 W → 0 W")
    changes.click()
    page.locator("#listView").click()
    matrix.locator('#tbody tr[data-port-key$="/1"] input').uncheck()
    page.locator("#matrixView").click()
    refresh_without_result(page, "2", "failed", matrix.locator('#matrix [data-port-key$="/2"]'))
    refresh_without_result(page, "4", "cancelled", page.locator('.matrix-tile[data-key$="|4"]'))
    page.locator('.matrix-tile[data-key$="|2"]').click()
    page.locator("#refreshPorts").click()
    expect(tiles.nth(1)).to_have_attribute("data-state", "limited", timeout=60000)
    expect(page.locator("#statusText")).to_contain_text("재조회 완료")
    expect(tiles.nth(3)).to_have_attribute("data-phase", "stale")
    tiles.nth(3).click()
    page.locator("#refreshPorts").click()
    expect(page.locator("#matrixViewer")).to_be_visible(timeout=60000)
    expect(result_tiles.nth(3)).to_have_attribute("data-state", "limited")
    expect(page.locator("#matrixGroups")).to_be_hidden()
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    engine = sys.argv[1] if len(sys.argv) > 1 else "chromium"
    run_browser_case(
        exercise, switch_factory=PowerMatrixSwitch, browser_engine=engine, touch=engine == "webkit"
    )
    print(
        "PASS three power states, unmeasured and in-progress ports, filters, details and restoration of live observations"
    )

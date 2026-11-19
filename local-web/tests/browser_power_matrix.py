"""Compare both browser matrices against the same five switch-port observations."""

from threading import Event

from browser_qa import run_browser_case
from playwright.sync_api import expect
from verify_http_ssh import MonitorFixture, interface_status_table


class PowerMatrixSwitch(MonitorFixture):
    def __init__(self):
        super().__init__()
        self.draws = {"1": 0, "2": 9, "3": 8, "4": 8, "5": None}
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
            if port != "4":
                lines.append(f" System Name :matrix-ap-{port}")
        else:
            return super()._respond(channel, command)
        with self._lock:
            self.commands.append(command)
        channel.sendall("\r\n".join([command, *lines, self.prompt]).encode())
        return False


def refresh_without_result(page, port, final_state):
    """A submitted refresh can finish without an authoritative rows array."""
    jobs = "**/api/jobs"
    poll = "**/api/jobs/power-regression-*"
    page.route(jobs, lambda route: route.fulfill(json={"id": "power-regression-" + final_state}))
    page.route(
        poll, lambda route: route.fulfill(json={"state": final_state, "error": "재조회 실패"})
    )
    try:
        page.locator("#portLiveTab").click()
        tile = page.locator(f'.matrix-tile[data-key$="|{port}"]')
        tile.click()
        page.locator("#refreshPorts").click()
        expect(tile).to_have_attribute("data-phase", "stale")
        expect(tile).to_have_attribute("data-state", "unclassified")
        expect(page.locator("#showMatrix")).to_be_hidden()
        page.locator("#portControlTab").click()
        row = page.locator(f'#portTable tr[data-key$="|{port}"]')
        expect(row.locator("input[type=checkbox]")).to_be_disabled()
        expect(row.locator("td").nth(5)).to_have_text("—")  # Draw
        expect(row.locator("td").nth(8)).to_have_text("—")  # LLDP name
        expect(row.locator(".power-badge")).to_have_attribute("data-state", "unclassified")
        expect(page.locator('#portTable tr[data-key$="|1"] input')).to_be_enabled()
    finally:
        page.unroute(jobs)
        page.unroute(poll)


def exercise(page, url, switch, _second, _shots, _backup):
    page.goto(url)
    page.locator('nav a[href="#connect"]').click()
    page.locator("#username").fill(switch.username)
    page.locator("#password").fill(switch.password)
    page.locator("#loginForm button.primary").click()
    expect(page.locator("#connectionState")).to_contain_text(switch.username)
    page.locator('nav a[href="#ports"]').click()
    page.locator("#portLiveTab").click()
    page.locator("#queryTargets").fill(f"127.0.0.1:{switch.port} 1-5")
    page.locator("#queryForm .primary").click()
    csv = page.frame_locator("#csvViewer")
    csv_tiles = csv.locator("#matrix [data-port-key]")
    expected = ["unpowered", "limited", "normal", "limited", "unclassified"]
    expect(csv_tiles).to_have_count(5, timeout=90000)
    assert csv_tiles.evaluate_all("tiles => tiles.map(tile => tile.dataset.state)") == expected
    for selector, value in (
        ("mUnpowered", "1"),
        ("mLimited", "2"),
        ("mNormal", "1"),
        ("issueCount", "3포트"),
    ):
        expect(csv.locator("#" + selector)).to_have_text(value)
    expect(csv.locator("#mUnclassified")).to_contain_text("1개")
    csv.locator("#status").select_option("limited")
    expect(csv_tiles).to_have_count(2)
    csv.locator("#status").select_option("all")
    csv.locator("#priorityButton").click()
    expect(csv.locator("#extraView .dr-list button")).to_have_count(3)
    csv.locator("#navOverview").click()

    page.locator("#portLiveTab").click()
    tiles = page.locator(".matrix-tile")
    assert tiles.evaluate_all("tiles => tiles.map(tile => tile.dataset.state)") == expected
    expect(page.locator("#matrixLegend b")).to_have_text(["1", "2", "1"])
    expect(page.locator("#matrixCounts")).to_contain_text("미분류 1")
    page.locator("#matrixPriority").click()
    expect(tiles).to_have_count(3)
    page.locator("#matrixPriority").click()
    page.locator("#portFilter").select_option("normal")
    expect(tiles).to_have_count(1)
    expect(tiles).to_have_text("3")
    tiles.click()
    expect(page.locator("#matrixDetails")).to_contain_text("Down")
    expect(page.locator("#matrixDetails")).to_contain_text("matrix-ap-3")
    page.locator("#portControlTab").click()
    expect(page.locator("#portTable .power-badge")).to_have_attribute("data-state", "normal")
    expect(page.locator("#rowDetails .power-badge")).to_have_attribute("data-state", "normal")
    page.locator("#portFilter").select_option("")
    page.locator("#portLiveTab").click()
    current = tiles.filter(has_text="3")
    switch.draws["3"] = 0
    switch.allow_lldp.clear()
    try:
        page.locator("#refreshPorts").click()
        expect(current).to_have_attribute("data-phase", "loading")
        expect(current).to_have_attribute("data-state", "unclassified")
        expect(tiles.nth(1)).to_have_attribute("data-state", "limited")
        expect(page.locator("#queryTargets")).not_to_be_editable()
        expect(page.locator("[data-import=queryTargets]")).to_be_disabled()
        page.locator("#portControlTab").click()
        current_row = page.locator('#portTable tr[data-key$="|3"]')
        expect(current_row.locator("input")).to_be_disabled()
        expect(current_row).not_to_contain_text("8 W")
        expect(current_row).not_to_contain_text("matrix-ap-3")
        expect(current_row).to_contain_text("0 W", timeout=12000)
    finally:
        switch.allow_lldp.set()
    expect(current).to_have_attribute("data-state", "unpowered", timeout=60000)
    expected[2] = "unpowered"
    page.reload()
    page.locator("#portLiveTab").click()
    expect(tiles).to_have_count(5)
    expect(tiles.nth(2)).to_have_attribute("data-state", "unpowered")
    assert tiles.evaluate_all("tiles => tiles.map(tile => tile.dataset.state)") == expected
    page.locator("#showMatrix").click()
    expect(csv_tiles).to_have_count(5)
    assert csv_tiles.evaluate_all("tiles => tiles.map(tile => tile.dataset.state)") == expected
    refresh_without_result(page, "2", "failed")
    refresh_without_result(page, "4", "cancelled")
    page.locator("#portLiveTab").click()
    page.locator('.matrix-tile[data-key$="|2"]').click()
    page.locator("#refreshPorts").click()
    expect(tiles.nth(1)).to_have_attribute("data-state", "limited", timeout=60000)
    expect(page.locator("#statusText")).to_contain_text("재조회 완료")
    expect(tiles.nth(3)).to_have_attribute("data-phase", "stale")
    expect(page.locator("#showMatrix")).to_be_hidden()
    tiles.nth(3).click()
    page.locator("#refreshPorts").click()
    expect(tiles.nth(3)).to_have_attribute("data-state", "limited", timeout=60000)
    expect(page.locator("#showMatrix")).to_be_visible()
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    run_browser_case(exercise, switch_factory=PowerMatrixSwitch)
    print(
        "PASS three power states, unmeasured and in-progress ports, filters, details and restored CSV/live parity"
    )

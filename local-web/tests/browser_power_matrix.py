"""Check the unified results and transient refresh states for five switch ports."""

from threading import Event

from browser_qa import enter_ssh_credentials, run_browser_case
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
        page.locator("#refreshPorts").click()
        tile = page.locator(f'.matrix-tile[data-key$="|{port}"]')
        expect(tile).to_have_attribute("data-phase", "stale")
        expect(tile).to_have_attribute("data-state", "unclassified")
        expect(page.locator("#csvViewer")).to_be_hidden()
        expect(page.locator("#showMatrix")).to_be_hidden()
        tile.click()
        detail = page.locator("#matrixDetails")
        expect(detail.get_by_role("checkbox")).to_be_disabled()
        expect(detail).not_to_contain_text(" W")
        expect(detail).not_to_contain_text("matrix-ap-")
        page.locator('.matrix-tile[data-key$="|1"]').click()
        expect(detail.get_by_role("checkbox")).to_be_enabled()
    finally:
        page.unroute(jobs)
        page.unroute(poll)


def exercise(page, url, switch, _second, _shots, _backup):
    page.goto(url)
    page.locator('nav a[href="#ports"]').click()
    page.locator("#portLiveTab").click()
    page.locator("#queryTargets").fill(f"127.0.0.1:{switch.port} 1-5")
    page.locator("#queryForm .primary").click()
    enter_ssh_credentials(page, switch)
    csv = page.frame_locator("#csvViewer")
    csv_tiles = csv.locator("#matrix [data-port-key]")
    expected = ["unpowered", "limited", "normal", "limited", "unclassified"]
    expect(csv_tiles).to_have_count(5, timeout=90000)
    assert csv_tiles.evaluate_all("tiles => tiles.map(tile => tile.dataset.state)") == expected
    for selector, value in (
        ("mUnpowered", "1"),
        ("mLimited", "2"),
        ("mNormal", "1"),
    ):
        expect(csv.locator("#" + selector)).to_have_text(value)
    expect(csv.locator("#mUnclassified")).to_contain_text("1개")
    csv.locator("#status").select_option("limited")
    expect(csv_tiles).to_have_count(2)
    csv.locator("#status").select_option("all")
    page.locator("#portReviewTab").click()
    expect(csv.locator("#extraView .dr-list button")).to_have_count(3)
    csv.locator("#reviewObservedPort").click()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    expect(csv.locator("#extraView")).to_be_visible()
    expect(page.locator("#controlPanel")).to_be_visible()
    assert "configure terminal" not in switch.commands
    page.locator("#clearSelection").click()
    page.locator("#portMonitorTab").click()
    csv.locator("#status").select_option("normal")
    expect(csv_tiles).to_have_count(1)
    csv_tiles.click()
    expect(csv.locator("#detail")).to_contain_text("Down")
    expect(csv.locator("#detail")).to_contain_text("matrix-ap-3")
    expect(csv.locator("#tbody .pillstat.normal")).to_be_visible()
    expect(csv.locator("#detail .badge.normal")).to_be_visible()
    csv.locator("#status").select_option("all")
    csv.locator('#matrix [data-port-key$="/3"]').click()
    tiles = page.locator(".matrix-tile")
    current = page.locator('.matrix-tile[data-key$="|3"]')
    switch.draws["3"] = 0
    switch.allow_lldp.clear()
    try:
        page.locator("#refreshPorts").click()
        expect(current).to_have_attribute("data-phase", "loading")
        expect(current).to_have_attribute("data-state", "unclassified")
        expect(tiles.nth(1)).to_have_attribute("data-state", "limited")
        expect(page.locator("#queryTargets")).not_to_be_editable()
        expect(page.locator("[data-import=queryTargets]")).to_be_disabled()
        expect(page.locator("#csvViewer")).to_be_hidden()
        current.click()
        detail = page.locator("#matrixDetails")
        expect(detail.get_by_role("checkbox")).to_be_disabled()
        expect(detail).not_to_contain_text("8 W")
        expect(detail).not_to_contain_text("matrix-ap-3")
        expect(detail).to_contain_text("0 W", timeout=12000)
    finally:
        switch.allow_lldp.set()
    expect(csv_tiles.nth(2)).to_have_attribute("data-state", "unpowered", timeout=60000)
    expect(page.locator("#csvViewer")).to_be_visible()
    expect(page.locator("#matrixGroups")).to_be_hidden()
    expected[2] = "unpowered"
    page.reload()
    expect(csv_tiles).to_have_count(5)
    assert csv_tiles.evaluate_all("tiles => tiles.map(tile => tile.dataset.state)") == expected
    page.locator("#portLiveTab").click()
    expect(page.locator("#matrixGroups")).to_be_hidden()
    page.locator("#portMonitorTab").click()
    expect(page.locator("#csvViewer")).to_be_visible()
    refresh_without_result(page, "2", "failed", csv.locator('#matrix [data-port-key$="/2"]'))
    refresh_without_result(page, "4", "cancelled", page.locator('.matrix-tile[data-key$="|4"]'))
    page.locator('.matrix-tile[data-key$="|2"]').click()
    page.locator("#refreshPorts").click()
    expect(tiles.nth(1)).to_have_attribute("data-state", "limited", timeout=60000)
    expect(page.locator("#statusText")).to_contain_text("재조회 완료")
    expect(tiles.nth(3)).to_have_attribute("data-phase", "stale")
    expect(page.locator("#showMatrix")).to_be_hidden()
    tiles.nth(3).click()
    page.locator("#refreshPorts").click()
    expect(page.locator("#csvViewer")).to_be_visible(timeout=60000)
    expect(csv_tiles.nth(3)).to_have_attribute("data-state", "limited")
    expect(page.locator("#matrixGroups")).to_be_hidden()
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    run_browser_case(exercise, switch_factory=PowerMatrixSwitch)
    print(
        "PASS three power states, unmeasured and in-progress ports, filters, details and restored CSV/live parity"
    )

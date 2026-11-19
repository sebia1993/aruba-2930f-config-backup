"""Compare physical link, independent of PoE, through the live query and refresh actions."""

from datetime import datetime

from browser_power_matrix import PowerMatrixSwitch
from browser_qa import enter_ssh_credentials, run_browser_case
from playwright.sync_api import expect
from verify_http_ssh import interface_status_table


class InterfaceStatusSwitch(PowerMatrixSwitch):
    def __init__(self):
        super().__init__()
        self.draws = {"1": 8, "10": 8, "2/1": 8}
        self.links = {"1": "Up", "10": "Down", "2/1": "Up"}
        self.reject_status = False
        self.reject_poe = False

    def status_lines(self):
        if self.reject_status:
            return ["Permission denied"]
        aliases = {"1": "Down Up", "10": "Up", "2/1": ""}
        return interface_status_table((p, aliases[p], value) for p, value in self.links.items())

    def _respond(self, channel, command):
        if command == "show power-over-ethernet 10" and self.reject_poe:
            lines = ["Permission denied"]
        elif command == "show interfaces brief":
            lines = [
                "Port Type | Alert Enabled Status Mode",
                "1 100/1000T | No Yes Down Auto",
                "10 100/1000T | No No Up Auto",
                "2/1 100/1000T | No Yes Down Auto",
            ]
        else:
            return super()._respond(channel, command)
        with self._lock:
            self.commands.append(command)
        channel.sendall("\r\n".join([command, *lines, self.prompt]).encode())
        return False


def assert_live_readings(page, switch, expected, *, poe_failed=None):
    matrix_view = page.frame_locator("#matrixViewer")
    endpoint = f"127.0.0.1:{switch.port}"
    page.locator("#portMonitorTab").click()
    page.locator("#operation").select_option("poe_off")
    page.locator("#selectEligible").click()
    for port, link in expected.items():
        table_row = matrix_view.locator(f'#tbody tr[data-port-key="{endpoint}/{port}"]')
        state = "unclassified" if port == poe_failed else "normal"
        expect(table_row.locator(".pillstat." + state)).to_be_visible()
        visible_link = "미확인" if link == "확인 불가" else link
        expect(table_row.get_by_role("cell", name=visible_link, exact=True)).to_be_visible()
        checkbox = table_row.get_by_role("checkbox")
        if port == poe_failed:
            expect(checkbox).to_be_disabled()
        if port == poe_failed or link != "Up":
            expect(checkbox).not_to_be_checked()
        elif port != "10":
            expect(checkbox).to_be_checked()
        table_row.locator("button.row-select").click()
        expect(matrix_view.locator("#detail")).to_contain_text(f"인터페이스 {port}")
    page.locator("#clearSelection").click()
    query = page.evaluate("fetch('/api/workspace').then(r => r.json()).then(w => w.query)")
    assert not query.get("files")
    rows = query["rows"]
    assert {r["interface"]: r["link_status"] for r in rows} == expected
    for row in rows:
        port = row["interface"]
        if expected[port] in ("Up", "Down"):
            observed = datetime.fromisoformat(row["link_observed_at"])
            assert observed <= datetime.fromisoformat(row["finished_at"]), row
        else:
            assert not row["link_observed_at"], row
        if port == poe_failed:
            assert row["status"] == "failed" and row["watts"] is None and row["poe_enabled"] is None
        else:
            assert row["status"] == "ok" and float(row["watts"]) == 8 and row["poe_enabled"] is True
            assert row["enabled"] is (port != "10")

    for port, link in expected.items():
        tile = matrix_view.locator(f'#matrix [data-port-key="{endpoint}/{port}"]')
        expect(tile).to_have_attribute(
            "data-state", "unclassified" if port == poe_failed else "normal"
        )
        tile.click()
        link_field = matrix_view.locator("#detail .dfield").filter(
            has=matrix_view.get_by_text("관측 당시 링크", exact=True)
        )
        expect(link_field.locator(".data")).to_have_text("미확인" if link == "확인 불가" else link)
        link_time = matrix_view.locator("#detail label").filter(has_text="링크 관측 시각")
        expect(link_time).to_have_count(int(link in ("Up", "Down")))


def exercise(page, url, switch, _other, _shots, _backup):
    page.goto(url)
    page.locator('nav a[href="#ports"]').click()
    page.locator("#queryTargets").fill(f"127.0.0.1:{switch.port} 1,10,2/1")
    page.locator("#queryForm .primary").click()
    enter_ssh_credentials(page, switch)
    expect(page.locator("#statusText")).to_contain_text("포트 조회 완료", timeout=90000)
    assert switch.commands.count("show interfaces brief") == 1
    assert switch.commands.count("show interfaces status") == 1
    assert_live_readings(page, switch, dict(switch.links))

    switch.links = {"1": "Down", "10": "Up"}  # The VSF port is missing from this fresh table.
    for index, expected in enumerate(
        (
            {"1": "Down", "10": "Up", "2/1": "확인 불가"},
            dict.fromkeys(switch.draws, "확인 불가"),
        ),
        start=2,
    ):
        switch.reject_status = index == 3
        page.frame_locator("#matrixViewer").locator("#device").select_option(
            f"127.0.0.1:{switch.port}"
        )
        page.locator("#refreshSwitch").click()
        expect(page.locator("#statusText")).to_contain_text("재조회 완료", timeout=90000)
        assert switch.commands.count("show interfaces status") == index
        assert_live_readings(page, switch, expected)
    switch.reject_status = False
    switch.reject_poe = True
    page.frame_locator("#matrixViewer").locator('#matrix [data-port-key$="/10"]').click()
    page.locator("#refreshPorts").click()
    expect(page.locator("#statusText")).to_contain_text("재조회 완료", timeout=90000)
    assert switch.commands.count("show interfaces status") == 4
    assert_live_readings(
        page, switch, {"1": "확인 불가", "10": "Up", "2/1": "확인 불가"}, poe_failed="10"
    )
    assert "configure terminal" not in switch.commands
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    run_browser_case(exercise, switch_factory=InterfaceStatusSwitch)
    print(
        "PASS interface status: conflicting brief values, VSF, refresh, missing row and CLI denial"
    )

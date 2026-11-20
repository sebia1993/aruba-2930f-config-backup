"""Compare physical link, independent of PoE, through the live query and refresh actions."""

import re
import sys
from datetime import datetime

from browser_power_matrix import PowerMatrixSwitch
from browser_qa import enter_ssh_credentials, open_port_controls, run_browser_case
from browser_unified_controls import resize_workspace
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
    open_port_controls(page)
    page.locator("#operation").select_option("poe_off")
    page.locator("#selectEligible").click()
    page.locator("#listView").click()
    for port, link in expected.items():
        table_row = matrix_view.locator(f'#tbody tr[data-port-key="{endpoint}/{port}"]')
        state = "unclassified" if port == poe_failed else "normal"
        expect(table_row.locator(".pillstat." + state)).to_be_visible()
        checkbox = table_row.get_by_role("checkbox")
        if port == poe_failed:
            expect(checkbox).to_be_disabled()
            expect(checkbox).not_to_be_checked()
        else:
            expect(checkbox).to_be_checked()
        table_row.locator("button.row-select").click()
        expect(matrix_view.locator("#detail")).to_contain_text(f"인터페이스 {port}")
        link_field = matrix_view.locator("#detail .dfield").filter(
            has=matrix_view.get_by_text("관측 당시 링크", exact=True)
        )
        expect(link_field.locator(".data")).to_have_text("미확인" if link == "확인 불가" else link)
    page.get_by_role("button", name="전체 선택 해제", exact=True).click()
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

    page.locator("#matrixView").click()
    for port, link in expected.items():
        tile = matrix_view.locator(f'#matrix [data-port-key="{endpoint}/{port}"]')
        caption = {"Up": "↑ Up", "Down": "↓ Down", "확인 불가": "—"}[link]
        expect(tile.get_by_text(caption, exact=True)).to_be_visible()
        expect(tile).to_have_accessible_name(
            re.compile("링크 " + (r"미(조회|확인)" if link == "확인 불가" else link))
        )
        expect(tile).to_have_attribute(
            "data-state", "unclassified" if port == poe_failed else "normal"
        )
        if port == poe_failed:
            expect(tile.locator(".port-failure-mark")).to_be_visible()
        border = tile.evaluate("el => getComputedStyle(el).borderTopColor")
        tile.click()
        link_field = matrix_view.locator("#detail .dfield").filter(
            has=matrix_view.get_by_text("관측 당시 링크", exact=True)
        )
        expect(link_field.locator(".data")).to_have_text("미확인" if link == "확인 불가" else link)
        link_time = matrix_view.locator("#detail label").filter(has_text="링크 관측 시각")
        expect(link_time).to_have_count(int(link in ("Up", "Down")))
        if port != poe_failed:
            # Opening an eligible tile also checks it. Leave later refreshes with a clean selection.
            expect(tile).to_have_attribute("aria-pressed", "true")
            expect(tile).to_have_css("border-top-color", border)
            tile.click()
            expect(tile).to_have_attribute("aria-pressed", "false")


def verify_link_layout(page, shots):
    frame = page.frame_locator("#matrixViewer")
    tiles = frame.locator("#matrix [data-port-key]")
    original_size = page.viewport_size
    original_theme = page.locator("html").get_attribute("data-theme")
    if page.locator("#controlPanel").evaluate("panel => panel.open"):
        page.locator("#controlHeading").click()
    for theme in ("light", "dark"):
        if page.locator("html").get_attribute("data-theme") != theme:
            page.locator("#themeToggle").click()
        expect(frame.locator("html")).to_have_attribute("data-theme", theme)
        for width, height in ((1366, 768), (320, 568)):
            resize_workspace(page, width, height)
            colors = {}
            for tile in tiles.all():
                ap = tile.locator(".tile-ap-name")
                link = tile.locator(".tile-link")
                expect(link).to_be_visible()
                assert (
                    ap.bounding_box()["y"] + ap.bounding_box()["height"]
                    <= link.bounding_box()["y"] + 1
                )
                assert link.evaluate("""label => {
                    const range = document.createRange();
                    range.selectNodeContents(label);
                    const text = range.getBoundingClientRect();
                    const box = label.closest('button').getBoundingClientRect();
                    return text.left >= box.left && text.right <= box.right &&
                        text.top >= box.top && text.bottom <= box.bottom + 1;
                }""")
                border = tile.evaluate("el => getComputedStyle(el).borderTopColor")
                red, green, _blue = map(int, re.findall(r"\d+", border)[:3])
                direction = link.inner_text()
                assert green > red if direction == "↑ Up" else red > green, (theme, border)
                colors[direction] = border
            assert len(colors) == 2
            assert frame.locator("html").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            if shots:
                tiles.first.scroll_into_view_if_needed()
                page.screenshot(path=shots / f"link-matrix-{width}-{theme}.png")
    resize_workspace(page, original_size["width"], original_size["height"])
    if page.locator("html").get_attribute("data-theme") != original_theme:
        page.locator("#themeToggle").click()


def exercise(page, url, switch, _other, shots, _backup):
    page.goto(url)
    page.locator('nav a[href="#ports"]').click()
    page.locator("#queryTargets").fill(f"127.0.0.1:{switch.port} 1,10,2/1")
    page.locator("#queryForm .primary").click()
    enter_ssh_credentials(page, switch)
    expect(page.locator("#statusText")).to_contain_text("포트 조회 완료", timeout=90000)
    assert switch.commands.count("show interfaces brief") == 1
    assert switch.commands.count("show interfaces status") == 1
    assert_live_readings(page, switch, dict(switch.links))
    verify_link_layout(page, shots)

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
        page.get_by_role("button", name="전체 선택", exact=True).click()
        page.get_by_role("button", name="선택 3포트 재조회", exact=True).click()
        expect(page.locator("#statusText")).to_contain_text("재조회 완료", timeout=90000)
        assert switch.commands.count("show interfaces status") == index
        assert_live_readings(page, switch, expected)
    switch.reject_status = False
    switch.reject_poe = True
    matrix = page.frame_locator("#matrixViewer").locator("#matrix")
    matrix.locator('[data-port-key$="/10"]').click()
    expect(matrix.locator('[aria-pressed="true"]')).to_have_count(1)
    commands_before_refresh = len(switch.commands)
    page.locator("#refreshPorts").click()
    expect(page.locator("#statusText")).to_contain_text("재조회 완료", timeout=90000)
    assert switch.commands.count("show interfaces status") == 4
    poe_reads = [
        command
        for command in switch.commands[commands_before_refresh:]
        if command.startswith("show power-over-ethernet ")
    ]
    assert poe_reads == ["show power-over-ethernet 10"], poe_reads
    assert_live_readings(
        page, switch, {"1": "확인 불가", "10": "Up", "2/1": "확인 불가"}, poe_failed="10"
    )
    assert "configure terminal" not in switch.commands
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    engine = sys.argv[1] if len(sys.argv) > 1 else "chromium"
    run_browser_case(
        exercise,
        switch_factory=InterfaceStatusSwitch,
        browser_engine=engine,
        touch=engine == "webkit",
    )
    print(
        "PASS interface status: conflicting brief values, VSF, refresh, missing row and CLI denial"
    )

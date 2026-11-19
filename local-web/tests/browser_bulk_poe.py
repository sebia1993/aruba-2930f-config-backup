"""Exercise range selection and lost PoE ON acknowledgements using loopback SSH only."""

from threading import Event
from unittest.mock import patch

from browser_qa import click_quick_poe, run_browser_case
from playwright.sync_api import expect
from verify_http_ssh import MonitorFixture, interface_status_table


class RangeSwitch(MonitorFixture):
    def __init__(self):
        super().__init__()
        self.power = {"1": False, "2": False}
        self.replacement_key = None
        self.auth_at_disconnect = 0
        self.stale_port = None
        self.linked = set()

    def _respond(self, channel, command):
        port = command.split()[-1]
        prompt = self.prompt
        if command == "show interfaces brief":
            lines = ["Port Type | Alert Enabled Status Mode"]
            lines.extend(f"{p} 100/1000T | No Yes Down Auto" for p in self.power)
        elif command == "show interfaces status":
            lines = interface_status_table(
                (p, "", "Up" if p in self.linked else "Down") for p in self.power
            )
        elif command.startswith("show power-over-ethernet ") and port in self.power:
            enabled = self.power[port]
            if port == self.stale_port:
                enabled = False
                self.stale_port = None
            lines = [
                f"Status and Configuration Information for port {port}",
                "Power Enable : " + ("Yes" if enabled else "No"),
                "PoE Port Status : Searching" if enabled else "PoE Port Status : Disabled",
                "PD Power Draw : 0.0 W",
            ]
        elif command == "show lldp info remote-device 2":
            lines = []
        elif command == "configure terminal":
            lines = []
            prompt = self.prompt[:-1] + "(config)#"
        elif command in ("interface 1 power-over-ethernet", "interface 2 power-over-ethernet"):
            port = command.split()[1]
            self.power[port] = True
            if port == "1":
                self.commands.append(command)
                self.auth_at_disconnect = len(self.auth_attempts)
                if self.replacement_key:
                    self.host_key = self.replacement_key
                channel.close()
                return False
            self.stale_port = port
            lines = []
            prompt = self.prompt[:-1] + "(config)#"
        else:
            return super()._respond(channel, command)
        self.commands.append(command)
        channel.sendall(("\r\n".join([command, *lines, prompt])).encode("utf-8"))
        return False


def cancel_before_off(page, switch, address):
    reading, resume = Event(), Event()
    respond = switch._respond

    def hold_status(channel, command):
        if command == "show power-over-ethernet 1":
            reading.set()
            assert resume.wait(30), "Test did not resume the SSH status read"
        return respond(channel, command)

    page.locator("#selectEligible").click()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    start = len(switch.commands)
    with (
        patch.object(switch, "_respond", hold_status),
        click_quick_poe(page, "poe_off", [(address, "1")]),
    ):
        try:
            assert reading.wait(30), "Quick OFF never reached the port state read"
            expect(page.locator("#quickOff")).to_be_disabled()
            with page.expect_response(lambda res: res.url.endswith("/cancel")) as cancellation:
                page.locator("#stopJob").click()
            assert cancellation.value.ok
        finally:
            resume.set()
        expect(page.locator("#stopJob")).to_be_hidden(timeout=90000)
    assert "configure terminal" not in switch.commands[start:]
    assert switch.power["1"]


def run_range(page, url, switch, second, shots, output):
    page.goto(url)
    page.locator('nav a[href="#connect"]').click()
    page.locator("#username").fill(switch.username)
    page.locator("#password").fill(switch.password)
    page.locator("#loginForm button.primary").click()
    expect(page.locator("#connectionState")).to_contain_text(switch.username)
    page.locator('nav a[href="#ports"]').click()
    for rotate_key in (False, True):
        # Simulate a pair of powered-off access ports without attached PDs.
        switch.linked.clear()
        switch.power = {"1": False, "2": False}
        if rotate_key:
            switch.replacement_key = second.host_key
            switch.prompt = "Aruba 2930F Edge#"
        start = len(switch.commands)
        page.locator("#portLiveTab").click()
        page.locator("#queryTargets").fill(f"127.0.0.1:{switch.port} 1-2")
        page.locator("#queryForm .primary").click()
        expect(page.locator("#statusText")).to_contain_text("포트 조회 완료", timeout=90000)
        expect(page.locator("#csvViewer")).to_be_visible()
        page.locator("#selectionScope").select_option("all")
        page.get_by_role("button", name="범위 전체 선택", exact=True).click()
        expect(page.locator("#selectedCount")).to_have_text("선택 2개")
        address = f"127.0.0.1:{switch.port}"
        targets = [(address, "1"), (address, "2")]
        if not rotate_key:
            with click_quick_poe(page, "poe_off", targets):
                expect(page.locator("#quickResult")).to_contain_text("이미 OFF 2개", timeout=90000)
            assert "configure terminal" not in switch.commands[start:]
            page.get_by_role("button", name="범위 전체 선택", exact=True).click()
        with click_quick_poe(page, "poe_on", targets):
            rows = page.locator("#controlResult tbody tr")
            expect(rows).to_have_count(2, timeout=90000)
        results = rows.all_text_contents()
        writes = [cmd for cmd in switch.commands[start:] if cmd.endswith(" power-over-ethernet")]
        if rotate_key:
            assert "검증 미완료" in results[0] and "일시 중지" in results[1], results
            assert writes == ["interface 1 power-over-ethernet"], writes
            assert len(switch.auth_attempts) == switch.auth_at_disconnect
        else:
            assert all("검증 완료" in row for row in results), results
            assert all("0 W" in row for row in results), results
            after_second_write = switch.commands.index("interface 2 power-over-ethernet", start) + 1
            assert switch.commands[after_second_write:].count("show power-over-ethernet 2") >= 2
            assert writes == ["interface 1 power-over-ethernet", "interface 2 power-over-ethernet"]
            # Both ports have PoE ON, but only port 1 has a live AP-535.
            switch.linked.add("1")
            page.locator("#portLiveTab").click()
            page.locator("#queryPorts").click()
            expect(page.locator("#statusText")).to_contain_text("포트 조회 완료", timeout=90000)
            page.locator("#operation").select_option("poe_off")
            page.locator("#selectEligible").click()
            expect(page.locator("#selectedCount")).to_have_text("선택 1개")
            expect(page.locator(f'#portTable tr[data-key="{address}|1"] input')).to_be_checked()
            page.get_by_role("button", name="범위 전체 선택", exact=True).click()
            baseline = len(switch.commands)
            with click_quick_poe(page, "poe_off", targets):
                blocked = page.locator("#quickResult tbody tr")
                expect(blocked).to_have_count(1, timeout=90000)
                expect(blocked).to_contain_text("Link Up")
            assert "configure terminal" not in switch.commands[baseline:]
            assert switch.power == {"1": True, "2": True}
            cancel_before_off(page, switch, address)

    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    run_browser_case(run_range, switch_factory=RangeSwitch)
    print(
        "PASS bulk PoE ON with lost SSH acknowledgement: read-only confirmation, no replay, changed-key halt"
    )

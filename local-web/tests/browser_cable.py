"""Exercise shared monitoring targets and cable TDR over a loopback SSH connection."""

import re
import sys
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
from verify_http_ssh import interface_status_table


class CableSwitch(PowerMatrixSwitch):
    def __init__(self):
        super().__init__()
        self.readings = {"1": "11m", "2": "12m", "9": "99m"}
        self.test_reply = "confirm"
        self.pending_reads = 0
        self.format_unknown = False
        self.final_pair_ready = Event()
        self.final_pair_ready.set()
        self.ap_names["1"] = None
        self.reject_clear = False
        self.tested = []
        self.confirmations = []
        self.confirmed = Event()
        self.release_result = Event()
        self.release_result.set()

    def status_lines(self):
        return interface_status_table(
            (port, "Up Down", "Down" if port == "1" else "Up") for port in self.draws
        )

    def _respond(self, channel, command):
        if command == "clear cable-diagnostics":
            with self._lock:
                self.commands.append(command)
            response = [command]
            if self.reject_clear:
                response.append("Invalid input: clear")
            else:
                self.readings.clear()
            response.append("edge-lab(config)#")
            channel.sendall("\r\n".join(response).encode())
            return False
        if not command.startswith(("show cable-diagnostics ", "test cable-diagnostics ")):
            return super()._respond(channel, command)
        port = command.rsplit(" ", 1)[-1]
        with self._lock:
            self.commands.append(command)
        if command.startswith("test "):
            if self.test_reply == "reject":
                channel.sendall("\r\n".join([command, "Invalid input: test", self.prompt]).encode())
                return False
            if self.test_reply in {"table", "completion"}:
                self.tested.append(port)
                self.readings[port] = "27m"
                if self.test_reply == "table":
                    # This inline result must trigger show, not supply the missing measurement.
                    output = [f"{port} 1-2 Short 77m", "3-6 OK 77m", "4-5 OK 77m", "7-8 OK 77m"]
                else:
                    output = ["Cable diagnostics completed."]
                channel.sendall("\r\n".join([command, *output, self.prompt]).encode())
                return False
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
                self.readings[port] = "17m"
                self.confirmed.set()
                self.release_result.wait(12)
                if self.test_reply == "disconnect":
                    self.readings[port] = "31m"
                    self.pending_reads = 1
                    channel.get_transport().close()
                    return False
                if self.test_reply == "echo-uppercase":
                    channel.sendall(b"Y")
            channel.sendall(b"\r\n" + self.prompt.encode())
        else:
            if self.pending_reads:
                self.pending_reads -= 1
                channel.sendall("\r\n".join([command, "Test in progress", self.prompt]).encode())
                return False
            if port not in self.readings:
                channel.sendall(
                    "\r\n".join([command, "No results available", self.prompt]).encode()
                )
                return False
            distance = self.readings[port]
            lines = [
                command,
                "Cable Diagnostic Status - Copper Ports",
                "       MDI    Cable       Cable Length or",
                "Port   Pair   Status      Distance to Fault",
                "----   -----  ----------- -----------------",
                f"{port:<7}1-2    Short       {distance}",
                "       3-6    OK          8m",
                "       4-5    Open        0m",
            ]
            if self.format_unknown:
                lines = [command, f"{port} 1-2 ???", "9 1-2 OK 399m"]
            elif self.final_pair_ready.is_set():
                lines.append("       7-8    OK          8m")
            else:
                # CLI output may contain an unrecognized pair and an unrelated port.
                lines += [f"{port} 7-8 ???", "9 1-2 OK 399m", "  7-8 OK 399m"]
            lines.append(self.prompt)
            channel.sendall("\r\n".join(lines).encode())
        return False


def exercise(page, url, switch, other, shots, _backup):
    endpoint = f"127.0.0.1:{switch.port}"
    other_endpoint = f"127.0.0.1:{other.port}"
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    touch = page.context.browser.browser_type.name == "webkit"
    if touch:
        resize_workspace(page, 390, 844)
    page.goto(url)
    nav = page.get_by_role("navigation", name="주 메뉴")
    cable = nav.get_by_role("link", name="케이블 진단", exact=True)
    ports = nav.locator('a[href="#ports"]')
    cable.click()
    workspace = page.locator('[data-page="cable"]')
    expect(workspace).to_be_visible()
    expect(cable).to_have_attribute("href", "#cable")
    expect(cable.locator('[aria-hidden="true"]')).to_be_visible()
    expect(workspace.get_by_role("heading", name="케이블 진단", exact=True)).to_be_visible()
    expect(page.locator("#crumb")).to_have_text("케이블 진단")
    expect(workspace.locator('textarea,input:not([type="hidden"])')).to_have_count(0)
    expect(workspace.get_by_role("button", name="저장 결과 조회")).to_have_count(0)
    test = workspace.locator("#cableTest")
    targets = workspace.locator("#cableTargets")
    results = workspace.locator("#cableResult")
    expect(test).to_be_disabled()

    def edit(text):
        workspace.locator("#cableEdit").click()
        expect(ports).to_have_attribute("aria-current", "page")
        edit_query_targets(page).fill(text)
        cable.click()

    review = page.locator("#review")

    def click(button):
        if touch:
            button.tap()
        else:
            button.click()

    def start():
        click(test)
        expect(review).to_be_visible()
        expect(review).to_contain_text("선택하지 않은 포트")
        expect(review).to_contain_text("링크")
        expect(results.locator("table")).to_have_count(0)
        click(page.locator("#cableConfirm"))

    def commands(start_at):
        return [
            cmd
            for cmd in switch.commands[start_at:]
            if "cable-diagnostics" in cmd or cmd in ("configure terminal", "end")
        ]

    def sequence(*port_numbers):
        steps = ["configure terminal", "clear cable-diagnostics", "end"]
        for port in port_numbers:
            steps += [f"test cable-diagnostics {port}", f"show cable-diagnostics {port}"]
        return steps

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
    click(test)
    expect(review).to_contain_text(endpoint)
    review.get_by_role("button", name="검토 닫기").click()
    assert commands(0) == [] and not switch.auth_attempts
    start()
    enter_ssh_credentials(page, switch, touch=touch)
    expect(results).to_contain_text(re.compile(r"17\s*m"), timeout=60000)
    expect(results).not_to_contain_text(re.compile(r"11\s*m"))
    expect(workspace.locator("#cableResultTitle")).to_have_text("이번 진단 결과")
    assert commands(0) == sequence(1)
    assert "9" not in switch.readings
    for pair in ("1-2", "3-6", "4-5", "7-8"):
        expect(results).to_contain_text(pair)

    # Rejecting clear leaves an old measurement on the switch, but no result on the page.
    before = len(switch.commands)
    switch.reject_clear = True
    try:
        start()
        failure = results.locator('[data-stage="clear"][data-status="failed"]')
        expect(failure).to_contain_text("초기화", timeout=60000)
        expect(results.locator("table")).to_have_count(0)
        assert switch.readings["1"] == "17m"
        assert "clear cable-diagnostics" in commands(before)
        assert not any(cmd.startswith(("test cable-", "show cable-")) for cmd in commands(before))
    finally:
        switch.reject_clear = False

    # A test may start and affect the link even if the completion prompt never
    # arrives. Recover only with show, without repeating clear, test or confirmation.
    for mode, length in (("disconnect", "31m"), ("echo-uppercase", "17m"), ("reject", None)):
        switch.test_reply = mode
        before = len(switch.commands)
        confirmations = len(switch.confirmations)
        try:
            start()
            if length is None:
                failure = results.locator('[data-stage="test"][data-status="failed"]')
                expect(failure).to_be_visible(timeout=60000)
                expect(results.locator("table")).to_have_count(0)
                assert commands(before) == sequence(1)[:-1]
                assert len(switch.confirmations) == confirmations
            else:
                card = results.locator('[data-stage="show"][data-status="measured"]')
                expect(card).to_contain_text(length, timeout=60000)
                sent = sequence(1)
                if mode == "disconnect":
                    sent.append("show cable-diagnostics 1")
                    if shots:
                        card.screenshot(path=shots / "cable-confirmation-recovery.png")
                assert commands(before) == sent
                assert switch.confirmations[confirmations:] == [b"y"]
        finally:
            switch.test_reply = "confirm"

    ports.click()
    expect(ports).to_have_attribute("aria-current", "page")
    edit_query_targets(page).fill(endpoint + " 1-2")
    page.locator("#queryPorts").click()
    matrix = page.frame_locator("#matrixViewer")
    tiles = matrix.locator("#matrix [data-port-key]")
    expect(tiles).to_have_count(2, timeout=90000)
    expect(tiles.first).to_have_attribute("data-state", "unpowered")
    tiles.first.click()
    expect(matrix.locator("#detail")).to_contain_text("Down")
    tiles.first.click()
    # Tile and list selection share the same cable-diagnostics targets.
    tiles.last.click()
    cable.click()
    before = len(switch.commands)
    expect(results.locator("table")).to_have_count(0)
    start()
    expect(results).to_contain_text("측정 완료", timeout=60000)
    assert commands(before) == sequence(2)
    ports.click()
    page.locator("#listView").click()
    port_two = matrix.locator(f'#tbody tr[data-port-key="{endpoint}/2"] input')
    expect(port_two).to_be_checked()
    matrix.locator(f'#tbody tr[data-port-key="{endpoint}/1"] input').check()
    expect(page.locator("#selectedCount")).to_have_text("선택 2개")
    cable.click()
    before = len(switch.commands)
    start()
    expect(results.locator("table")).to_have_count(2, timeout=90000)
    assert commands(before) == sequence(1, 2)
    assert switch.confirmations == [b"y"] * len(switch.tested)
    ports.click()
    port_two.uncheck()
    cable.click()
    expect(results.locator("table")).to_have_count(0)
    before = len(switch.commands)
    start()
    expect(results).to_contain_text(re.compile(r"17\s*m"), timeout=60000)
    assert commands(before) == sequence(1)

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

    # Port 1 is selected, draws 0 W, and its physical link is Down. Both ways of
    # acknowledging a test without a Y/N exchange must obtain fresh data from show.
    if touch:
        resize_workspace(page, 390, 844)
    for test_reply in ("table", "completion"):
        switch.test_reply = test_reply
        switch.final_pair_ready.clear()
        before = len(switch.commands)
        confirmations = len(switch.confirmations)
        try:
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/api/jobs")
                    and response.request.post_data_json.get("action") == "cable.test"
                )
            ) as created:
                start()
            job_url = created.value.url + "/" + created.value.json()["id"]
            card = results.locator(".cable-reading")
            expect(card).to_contain_text("3/4페어 확인", timeout=30000)
            expect(test).to_be_disabled()
            rows = card.locator("tbody tr")
            expect(rows).to_have_count(4)
            expect(rows.filter(has_text="4-5")).to_contain_text(re.compile(r"0\s*m"))
            expect(rows.filter(has_text="4-5")).to_contain_text("Open")
            expect(rows.filter(has_text="7-8")).to_contain_text("값 미수신")
            expect(card).to_contain_text("27m")
            expect(card).not_to_contain_text(re.compile(r"(17|77)\s*m"))
            if test_reply == "table":
                switch.final_pair_ready.set()
                expect(card).to_contain_text("측정 완료", timeout=30000)
                expect(rows.filter(has_text="7-8")).to_contain_text("8m")
                expect(card.locator("summary")).to_have_count(0)
            else:
                # Let this run reach its real read deadline with three usable pairs.
                expect(test).to_be_enabled(timeout=60000)
                expect(card).to_contain_text("3/4페어 확인")
                expect(rows.filter(has_text="7-8")).to_contain_text("값 미수신")
                disclosure = card.locator("details")
                expect(disclosure).not_to_have_attribute("open", "")
                click(disclosure.get_by_text("이번 응답 보기", exact=True))
                expect(disclosure).to_contain_text("7-8")
                expect(disclosure).not_to_contain_text("399m")
                # Another authorized viewer can read the partial measurements, but the
                # initiating screen alone receives the diagnostic text.
                shared_job = page.context.request.get(job_url)
                assert shared_job.ok
                peer_row = shared_job.json()["result"]["rows"][0]
                assert len(peer_row["pairs"]) == 3
                assert "responseExcerpt" not in shared_job.text()
                if touch:
                    resize_workspace(page, 320, 568)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if shots:
                    card.screenshot(path=shots / f"cable-partial-{page.viewport_size['width']}.png")
            expect(test).to_be_enabled()
            sent = commands(before)
            assert sent[:4] == sequence(1)[:4], sent
            reads = sent[4:]
            assert 1 < len(reads) <= 16 and all(
                cmd == "show cable-diagnostics 1" for cmd in reads
            ), sent
            assert len(switch.confirmations) == confirmations
            assert "y" not in switch.commands[before:]
        finally:
            switch.final_pair_ready.set()
            switch.test_reply = "confirm"

    before = len(switch.commands)
    switch.test_reply = "completion"
    switch.format_unknown = True
    try:
        start()
        unreadable = results.locator('[data-stage="show"][data-status="failed"]')
        expect(unreadable).to_contain_text("형식", timeout=30000)
        click(unreadable.get_by_text("이번 응답 보기", exact=True))
        expect(unreadable.locator("pre")).to_contain_text("1-2 ???")
        expect(unreadable.locator("pre")).not_to_contain_text("399m")
        expect(unreadable).not_to_contain_text("27m")
        assert commands(before) == sequence(1)
    finally:
        switch.format_unknown = False
        switch.test_reply = "confirm"

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
    expect(test).to_be_disabled()
    # A new page picks up the last saved monitoring inventory with its SSH port.
    page.reload()
    expect(targets).to_contain_text(endpoint)
    expect(results.locator("table")).to_have_count(0)
    # Reopening mid-run reconnects to that run. Identical pair readings after a
    # successful clear and new test are fresh, and reconnect never repeats them.
    ports.click()
    matrix.locator(f'#matrix [data-port-key="{endpoint}/1"]').click()
    cable.click()
    before = len(switch.commands)
    switch.confirmed.clear()
    switch.release_result.clear()
    try:
        start()
        assert switch.confirmed.wait(20), "Switch did not receive the approved test"
        # Reload between status requests. WebKit cancels a document's outstanding
        # fetch on navigation even though the server-side cable test continues.
        poll = (
            page.wait_for_event(
                "requestfinished", predicate=lambda r: r.method == "GET" and "/api/jobs/" in r.url
            )
            .response()
            .json()
        )
        assert poll["action"] == "cable.test" and poll["state"] == "running"
        page.reload()
        expect(page.locator("#statusText")).to_contain_text(endpoint)
    finally:
        switch.release_result.set()
    expect(workspace.locator("#cableResultTitle")).to_contain_text("재연결한 작업", timeout=60000)
    expect(results).to_contain_text("측정 완료")
    expect(results).to_contain_text(re.compile(r"17\s*m"))
    assert commands(before) == sequence(1)
    assert not other.commands and not other.auth_attempts
    assert not errors, errors
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    engine = sys.argv[1] if len(sys.argv) > 1 else "chromium"
    run_browser_case(
        exercise, switch_factory=CableSwitch, browser_engine=engine, touch=engine == "webkit"
    )
    print(
        f"PASS {engine}: shared cable targets, clear once then test/show, fresh results, failed clear and active-job recovery."
    )

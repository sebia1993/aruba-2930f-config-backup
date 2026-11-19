"""Verify the query workspace and selective retry through local HTTP and SSH."""

import re
import sys
from threading import Event

from browser_power_matrix import PowerMatrixSwitch
from browser_qa import enter_ssh_credentials, run_browser_case
from browser_unified_controls import hit_point, resize_workspace
from playwright.sync_api import expect


class QuerySwitch(PowerMatrixSwitch):
    def __init__(self):
        super().__init__()
        self.failed_ports = {"1", "2"}
        self.failed_lldp = set()
        self.draws["5"] = 0
        self.read_started = Event()
        self.release_read = Event()

    def _respond(self, channel, command):
        port = command.split()[-1]
        response = None
        if command.startswith("show lldp info remote-device "):
            if port in self.failed_lldp:
                response = "Permission denied"
            elif port == "5":
                response = "LLDP Remote Device Information Detail\r\nNo neighbors found."
        if command.startswith("show power-over-ethernet "):
            self.read_started.set()
            self.release_read.wait(20)
            if port in self.failed_ports:
                response = "Permission denied"
            elif port == "4":
                response = "This port does not support PoE."
        if response is not None:
            with self._lock:
                self.commands.append(command)
            channel.sendall(("\r\n".join([command, response, self.prompt])).encode())
            return False
        return super()._respond(channel, command)


def current_rows(page):
    rows = page.evaluate("async () => (await (await fetch('/api/workspace')).json()).query.rows")
    return {(row["ip"], row["ssh_port"], row["interface"]): row for row in rows}


def check_matrix_at_top(page):
    workspace = page.locator("#matrixWorkspace")
    expect(page.locator("[data-page=ports]")).to_have_class(re.compile(r"\bmatrix-first\b"))
    expect(page.locator("#queryTargets")).to_be_hidden()
    expect(page.locator("#monitorInterval")).to_be_hidden()
    expect(workspace.locator("#matrixWorkspaceTitle")).to_be_in_viewport()
    expect(workspace.locator("#queryPorts")).to_be_visible()
    assert workspace.evaluate("""el => {
        const page = el.closest("[data-page=ports]");
        const top = page.getBoundingClientRect().top + parseFloat(getComputedStyle(page).paddingTop);
        return Math.abs(el.getBoundingClientRect().top - top) <= 2 &&
            document.documentElement.scrollWidth <= innerWidth && page.scrollWidth <= page.clientWidth + 1;
    }""")


def exercise(page, url, switch, other, shots, _backup):
    mobile = page.context.browser.browser_type.name == "webkit"
    width, height = (390, 844) if mobile else (1366, 768)
    resize_workspace(page, width, height)
    page.goto(url)
    address, other_address = (f"127.0.0.1:{peer.port}" for peer in (switch, other))
    draft = f"\n  {address}   1-2  \n"
    input_field, submit = page.locator("#queryTargets"), page.locator("#queryPorts")
    submit.click()
    expect(input_field).to_be_visible()
    expect(page.locator("#queryCredentials")).to_be_hidden()
    input_field.fill(draft)
    submit.click()
    expect(page.locator("#queryCredentials")).to_be_visible()
    expect(input_field).to_be_visible()
    expect(page.locator("[data-page=ports].matrix-first")).to_have_count(0)
    page.locator("#cancelQueryCredentials").click()
    expect(input_field).to_have_value(draft)
    submit.click()
    try:
        enter_ssh_credentials(page, switch)
        assert switch.read_started.wait(15), "SSH query did not start"
        check_matrix_at_top(page)
        expect(page.locator(".matrix-tile")).to_have_count(2)
        hit_point(page.locator(".matrix-tile").first)
        if shots:
            page.screenshot(path=shots / f"matrix-first-running-{width}.png")
    finally:
        switch.release_read.set()

    retry = page.locator("#retryFailedPorts")
    expect(input_field).to_be_editable(timeout=60000)
    expect(input_field).to_be_visible()
    expect(input_field).to_have_value(draft)
    expect(page.locator("[data-page=ports].matrix-first")).to_have_count(0)
    expect(retry).to_be_hidden()
    assert [r["status"] for r in current_rows(page).values()] == ["failed", "failed"]
    page.reload()
    expect(input_field).to_have_value(draft)
    expect(input_field).to_be_editable()
    expect(page.locator("[data-page=ports].matrix-first")).to_have_count(0)

    # A switch reporting a real link on a non-PoE port is a completed query.
    input_field.fill(f"{address} 4")
    submit.click()
    expect(submit).to_be_enabled(timeout=60000)
    check_matrix_at_top(page)
    expect(retry).to_be_hidden()
    unsupported = current_rows(page)[("127.0.0.1", switch.port, "4")]
    assert unsupported["status"] == "unsupported" and unsupported["link_status"] == "Up"
    settings = page.locator("#editQueryTargets")
    settings.click()

    # Both switches have an interface 1 at the same IP. Only one of these fails.
    switch.failed_ports = {"1"}
    switch.failed_lldp = {"2"}
    input_field.fill(f"{address} 1-5\n{other_address} 1")
    submit.click()
    expect(retry).to_be_visible(timeout=60000)
    expect(retry).to_be_enabled()
    check_matrix_at_top(page)
    hit_point(settings)
    settings.click()
    expect(input_field).to_be_editable()
    expect(page.locator("#monitorInterval")).to_be_visible()
    settings.click()
    check_matrix_at_top(page)
    if shots:
        page.screenshot(path=shots / f"matrix-first-partial-{width}.png")
    if mobile:
        resize_workspace(page, 320, 568)
        check_matrix_at_top(page)
        hit_point(page.frame_locator("#matrixViewer").locator("#matrix [data-port-key]").first)
        assert page.locator("[data-page=ports]").evaluate("el => !window.scrollY && !el.scrollTop")
        if shots:
            page.screenshot(path=shots / "matrix-first-partial-320.png")
        resize_workspace(page, width, height)

    before = current_rows(page)
    failed = ("127.0.0.1", switch.port, "1")
    failed_neighbor = ("127.0.0.1", switch.port, "2")
    unchanged = {("127.0.0.1", switch.port, p) for p in ("3", "4", "5")}
    unchanged.add(("127.0.0.1", other.port, "1"))
    assert set(before) == unchanged | {failed, failed_neighbor}
    assert before[failed]["status"] == "failed"
    assert (
        before[failed_neighbor]["status"] == "ok"
        and before[failed_neighbor]["lldp_status"] == "failed"
    )
    unpowered = before[("127.0.0.1", switch.port, "5")]
    assert float(unpowered["watts"]) == 0 and unpowered["lldp_status"] == "ok"
    assert unpowered["lldp_neighbors"] == []
    first_log, second_log = len(switch.commands), len(other.commands)
    page.locator("#listView").click()
    frame = page.frame_locator("#matrixViewer")
    device_filter = frame.locator("#device")
    if not device_filter.is_visible():
        frame.locator("#shortFilterToggle").click()
    device_filter.select_option(other_address)
    selected = frame.locator(f'#tbody tr[data-port-key="{other_address}/1"] input')
    selected.check()

    # Retrying while a filter hides the failure must address that failure only.
    switch.failed_ports.clear()
    for retry_ports in ("1,2", "2"):
        if retry_ports == "2":
            switch.failed_lldp.clear()
        with page.expect_request(
            lambda req: (
                req.url.endswith("/api/jobs")
                and req.method == "POST"
                and req.post_data_json["action"] == "ports.refresh"
            )
        ) as request:
            retry.click()
        assert request.value.post_data_json["body"]["targets"] == f"{address} {retry_ports}"
        expect(submit).to_be_enabled(timeout=60000)
        expect(page.locator("#listView")).to_have_attribute("aria-pressed", "true")
        expect(device_filter).to_have_value(other_address)
        expect(selected).to_be_checked()
        results = current_rows(page)
        assert results[failed]["status"] == "ok"
        assert results[failed_neighbor]["lldp_status"] == ("failed" if switch.failed_lldp else "ok")
        for key in unchanged:
            assert results[key]["observed_at"] == before[key]["observed_at"]
            assert results[key]["watts"] == before[key]["watts"]
        if switch.failed_lldp:
            expect(retry).to_be_enabled()
    expect(retry).to_be_hidden()
    retried = switch.commands[first_log:]
    assert retried.count("show power-over-ethernet 1") == 1
    assert retried.count("show power-over-ethernet 2") == 2
    for port in ("3", "4", "5"):
        assert f"show power-over-ethernet {port}" not in retried
        assert f"show lldp info remote-device {port}" not in retried
    assert len(other.commands) == second_log
    assert all("configure terminal" not in peer.commands for peer in (switch, other))
    resize_workspace(page, 1366, 768)
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    engine = sys.argv[1] if len(sys.argv) > 1 else "chromium"
    run_browser_case(exercise, switch_factory=QuerySwitch, browser_engine=engine)
    print(
        f"PASS {engine}: matrix-first at SSH query start, editable all-failure draft, exact failed-port retries and preserved filters, readings and selection."
    )

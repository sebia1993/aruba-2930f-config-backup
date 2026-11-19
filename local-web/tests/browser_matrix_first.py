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


def retry_from_tile(page, frame, address, port, reason, mobile, shots):
    tile = frame.locator(f'#matrix [data-port-key="{address}/{port}"]')

    def activate(element):
        if mobile:
            element.tap()
        else:
            element.click()

    activate(tile)
    detail = frame.locator("#detail")
    failure = detail.locator("#portReadFailure")
    expect(failure).to_be_visible()
    expect(failure).to_contain_text("LLDP 조회 실패" if port == "2" else "포트 조회 실패")
    expect(failure).to_contain_text(reason)
    button = detail.get_by_role("button", name="이 포트만 재조회", exact=True)
    expect(button).to_be_enabled()
    hit_point(button)
    if shots:
        page.screenshot(path=shots / f"port-{port}-failure-{page.viewport_size['width']}.png")
    with page.expect_request(
        lambda request: request.url.endswith("/api/jobs") and request.method == "POST"
    ) as sent:
        activate(button)
    payload = sent.value.post_data_json
    assert (
        payload["action"] == "ports.refresh" and payload["body"]["targets"] == f"{address} {port}"
    )
    expect(page.locator("#queryPorts")).to_be_enabled(timeout=60000)


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


def toggle_failures(count, mobile):
    if mobile:
        count.tap()
    else:
        count.focus()
        count.press("Space")


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
    frame = page.frame_locator("#matrixViewer")
    first_failure = frame.locator(f'#matrix [data-port-key="{address}/1"]')
    expect(first_failure).to_have_accessible_name(re.compile("조회 실패"))
    markers = frame.locator('#matrix [data-read-failed="true"]')
    expect(markers).to_have_count(2)
    failure_count = frame.get_by_role("button", name=re.compile(r"조회 실패\s*\d+개"))
    expect(failure_count).to_have_attribute("aria-pressed", "false")
    for port in ("1", "2"):
        expect(
            frame.locator(f'#matrix [data-port-key="{address}/{port}"] .port-failure-mark')
        ).to_be_visible()
    for key in (f"{address}/3", f"{address}/4", f"{address}/5", f"{other_address}/1"):
        expect(frame.locator(f'#matrix [data-port-key="{key}"]')).not_to_have_accessible_name(
            re.compile("조회 실패")
        )
    expect(frame.locator(f'#matrix [data-port-key="{address}/5"]')).to_have_attribute(
        "data-state", "unpowered"
    )
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
        first_failure.tap()
        expect(page.locator("#portActionStrip")).to_be_visible()
        detail_retry = frame.locator("#detail").get_by_role("button", name="이 포트만 재조회")
        expect(detail_retry).to_be_in_viewport()
        resize_workspace(page, 320, 568)
        hit_point(detail_retry)
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
    device_filter = frame.locator("#device")
    if not device_filter.is_visible():
        frame.locator("#shortFilterToggle").click()
    device_filter.select_option(other_address)
    selected = frame.locator(f'#tbody tr[data-port-key="{other_address}/1"] input')
    selected.check()
    expect(failure_count).to_be_hidden()

    # The checked port shares an IP and interface number with the failed port.
    # The explicit detail action must ignore that selection.
    device_filter.select_option(address)
    search = frame.locator("#search")
    search.fill("127.0.0.1")
    page.locator("#matrixView").click()
    if mobile:
        resize_workspace(page, 320, 568)
    original = frame.locator(f'#matrix [data-port-key="{address}/5"]')
    original.click()
    expect(original).to_have_attribute("aria-pressed", "true")
    tiles = frame.locator("#matrix [data-port-key]")
    expect(tiles).to_have_count(5)
    filters = frame.locator("#shortFilterToggle")
    if mobile:
        expect(filters).to_have_attribute("aria-expanded", "true")
    requests = []

    def record_job(req):
        if req.method == "POST" and req.url.endswith("/api/jobs"):
            requests.append(req.post_data_json)

    page.on("request", record_job)
    toggle_failures(failure_count, mobile)
    expect(failure_count).to_have_attribute("aria-pressed", "true")
    expect(failure_count).to_have_text(re.compile("조회 실패 2개"))
    expect(tiles).to_have_count(2)
    assert set(tiles.evaluate_all("els => els.map(el => el.dataset.portKey)")) == {
        f"{address}/1",
        f"{address}/2",
    }
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    if mobile:
        hit_point(first_failure)
    if shots:
        page.screenshot(path=shots / f"failed-only-{page.viewport_size['width']}.png")
    first_failure.click()
    expect(frame.locator("#portReadFailure")).to_be_visible()
    toggle_failures(failure_count, mobile)
    expect(failure_count).to_have_attribute("aria-pressed", "false")
    expect(tiles).to_have_count(5)
    expect(original).to_have_attribute("aria-pressed", "true")
    expect(frame.locator("#portReadFailure")).to_be_hidden()
    expect(device_filter).to_have_value(address)
    expect(search).to_have_value("127.0.0.1")
    if mobile:
        expect(filters).to_have_attribute("aria-expanded", "true")
        expect(search).to_be_visible()
    expect(page.locator("#matrixView")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    page.remove_listener("request", record_job)
    assert requests == []
    assert (len(switch.commands), len(other.commands)) == (first_log, second_log)
    device_filter.select_option("")
    search.fill("")
    retry_from_tile(page, frame, address, "1", "show power-over-ethernet 1", mobile, shots)
    expect(markers).to_have_count(2)
    expect(first_failure).to_have_accessible_name(re.compile("조회 실패"))
    expect(frame.locator("#detail #portReadFailure")).to_contain_text("show power-over-ethernet 1")
    assert switch.commands[first_log:].count("show power-over-ethernet 1") == 1
    assert "show power-over-ethernet 2" not in switch.commands[first_log:]
    assert len(other.commands) == second_log
    after_one = current_rows(page)
    for key in unchanged | {failed_neighbor}:
        assert after_one[key]["observed_at"] == before[key]["observed_at"]
        assert after_one[key]["watts"] == before[key]["watts"]
    if mobile:
        resize_workspace(page, width, height)
    page.locator("#listView").click()
    if not device_filter.is_visible():
        frame.locator("#shortFilterToggle").click()
    device_filter.select_option(other_address)
    expect(selected).to_be_checked()

    # Retrying while a filter hides the failure must address that failure only.
    def retry_failures(retry_ports):
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

    retry_failures("1,2")
    expect(page.locator("#listView")).to_have_attribute("aria-pressed", "true")
    expect(device_filter).to_have_value(other_address)
    expect(selected).to_be_checked()
    results = current_rows(page)
    assert (
        results[failed]["status"] == "failed"
        and results[failed_neighbor]["lldp_status"] == "failed"
    )
    device_filter.select_option("")
    page.locator("#matrixView").click()
    expect(markers).to_have_count(2)
    retry_from_tile(page, frame, address, "2", "show lldp info remote-device 2", mobile, shots)
    status = page.locator("#statusText")
    expect(status).to_contain_text("LLDP 조회 실패")
    expect(status).to_contain_text(f"{address} 포트 2")
    expect(status).not_to_contain_text("show power-over-ethernet 1")
    expect(markers).to_have_count(2)
    original.click()
    toggle_failures(failure_count, mobile)
    expect(failure_count).to_have_attribute("aria-pressed", "true")
    switch.failed_lldp.clear()
    retry_from_tile(page, frame, address, "2", "show lldp info remote-device 2", mobile, shots)
    expect(status).to_contain_text(re.compile("완료|성공"))
    expect(status).to_contain_text(f"{address} 포트 2")
    expect(status).not_to_contain_text("실패")
    expect(markers).to_have_count(1)
    expect(failure_count).to_have_text(re.compile("조회 실패 1개"))
    expect(tiles).to_have_count(1)
    expect(first_failure).to_have_attribute("data-read-failed", "true")
    expect(frame.locator("#detail #portReadFailure")).to_be_hidden()
    expect(frame.get_by_role("button", name="이 포트만 재조회")).to_be_hidden()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    recovered = current_rows(page)
    assert recovered[failed_neighbor]["lldp_status"] == "ok"
    for key in unchanged | {failed}:
        assert recovered[key]["observed_at"] == results[key]["observed_at"]
        assert recovered[key]["watts"] == results[key]["watts"]
    switch.failed_ports.clear()
    retry_failures("1")
    expect(retry).to_be_hidden()
    expect(failure_count).to_have_attribute("aria-pressed", "true")
    expect(failure_count).to_have_text(re.compile("조회 실패 0개"))
    expect(tiles).to_have_count(0)
    toggle_failures(failure_count, mobile)
    expect(tiles).to_have_count(6)
    expect(original).to_have_attribute("aria-pressed", "true")
    expect(failure_count).to_be_hidden()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    page.locator("#listView").click()
    expect(selected).to_be_checked()
    final = current_rows(page)
    assert final[failed]["status"] == "ok"
    for key in unchanged:
        assert final[key]["observed_at"] == before[key]["observed_at"]
        assert final[key]["watts"] == before[key]["watts"]
    retried = switch.commands[first_log:]
    assert retried.count("show power-over-ethernet 1") == 3
    assert retried.count("show power-over-ethernet 2") == 3
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
    run_browser_case(
        exercise, switch_factory=QuerySwitch, browser_engine=engine, touch=engine == "webkit"
    )
    print(
        f"PASS {engine}: matrix-first query, reversible failure-count filter, exact single and bulk retries, preserved readings and selection."
    )

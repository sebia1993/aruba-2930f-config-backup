"""Exercise matrix selection, filtered bulk controls and exact SSH endpoints on loopback."""

from threading import Event
from unittest.mock import patch

from browser_qa import click_quick_poe, enter_ssh_credentials, expect_power_result, run_browser_case
from playwright.sync_api import expect


def change_target_while_previewing(page, switch, address):
    reading, resume = Event(), Event()
    respond = switch._respond

    def pause_power_read(channel, command):
        if command == "show power-over-ethernet 1":
            reading.set()
            assert resume.wait(30), "Test did not release the port read"
        return respond(channel, command)

    page.locator("#controlMode").select_option("single")
    page.locator("#singleIP").fill(address)
    port = page.locator("#singlePort")
    port.fill("1")
    start = len(switch.commands)
    with patch.object(switch, "_respond", pause_power_read):
        try:
            page.locator("#preview").click()
            assert reading.wait(30), "Single-port preview did not reach PoE read"
            port.fill("2")
        finally:
            resume.set()
        expect(page.locator("#controlResult")).to_contain_text(
            "현재 선택으로 다시 검토", timeout=60000
        )
    expect(page.locator("#review")).to_be_hidden()
    expect(port).to_have_value("2")
    commands = switch.commands[start:]
    assert "show power-over-ethernet 1" in commands and "configure terminal" not in commands
    assert switch.poe_enabled


def select_measured_port(page, csv_view, changed, available, untouched_switch):
    page.locator("#portLiveTab").click()
    page.locator("#queryPorts").click()
    changed_row = csv_view.locator(f'#tbody tr[data-port-key="{changed}/1"] input')
    other_row = csv_view.locator(f'#tbody tr[data-port-key="{available}/1"] input')
    expect(changed_row).to_be_enabled(timeout=60000)
    changed_row.check()
    untouched_commands = len(untouched_switch.commands)
    with click_quick_poe(page, "poe_on", [(changed, "1")]):
        result = page.locator("#controlResult tbody tr")
        expect(result).to_have_count(1, timeout=90000)
        expect(result).to_contain_text(changed)
        expect(result).to_contain_text("검증 완료")
    expect_power_result(page, changed, "0 W", "9.7 W", delta="+9.7 W")
    expect(csv_view.locator(f'#tbody tr[data-port-key="{changed}/1"]')).to_contain_text("9.7 W")
    assert len(untouched_switch.commands) == untouched_commands
    expect(changed_row).to_be_enabled()
    expect(other_row).to_be_enabled()

    csv_view.locator(f'#matrix [data-port-key="{changed}/1"]').click()
    csv_view.locator("#inspectObservedPort").click()
    mode = page.locator("#controlMode")
    expect(mode).to_have_value("batch")
    expect(changed_row).to_be_checked()
    expect(other_row).not_to_be_checked()
    expect(page.locator("#controlTarget")).to_contain_text(changed)


def leave_failed_port_inspection_for_batch(page, csv_view, failed, available):
    csv_view.locator(f'#matrix [data-port-key="{failed}/1"]').click()
    csv_view.locator("#inspectObservedPort").click()
    mode = page.locator("#controlMode")
    expect(mode).to_have_value("single", timeout=60000)
    expect(mode).to_be_disabled()
    expect(page.locator("#singleIP")).to_have_value(failed)
    expect(page.locator("#singlePort")).not_to_be_editable()
    expect(page.locator("#liveScope")).to_be_hidden()

    csv_view.locator(f'#tbody tr[data-port-key="{available}/1"] input').check()
    expect(mode).to_have_value("batch")
    expect(mode).to_be_enabled()
    expect(page.locator("#liveScope")).to_be_visible()
    expect(csv_view.locator("#tbody")).to_be_visible()
    expect(page.locator("#controlTarget")).to_contain_text(available)
    mode.select_option("single")
    expect(page.locator("#singleIP")).to_be_editable()
    expect(page.locator("#singlePort")).to_be_editable()


def exercise(page, url, switch, second, shots, _backup):
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.goto(url)
    page.get_by_role("link", name="포트 관제·제어").click()
    combined = page.locator("#portMonitorTab")
    expect(combined).to_have_accessible_name("관제")
    workspace = page.locator('[data-page="ports"]')
    for name in ("관제", "우선 확인", "변화 비교", "실시간 조회", "CSV 열기"):
        expect(workspace.get_by_role("button", name=name, exact=True)).to_have_count(1)
    for selector in (
        "#portTable",
        "#rowDetails",
        "#controlRows",
        "#portSearch",
        "#jumpToControl",
        "#returnToMonitor",
    ):
        expect(workspace.locator(selector)).to_have_count(0)
    expect(page.locator("#portControlTab")).to_have_count(0)
    expect(page.locator("#controlPanel")).to_be_visible()
    expect(page.locator("#singleControls")).to_be_visible()
    expect(page.locator("#singleIP")).to_be_editable()
    csv_view = page.frame_locator("#csvViewer")
    expect(csv_view.locator("#matrix [data-port-key]")).to_have_count(0)
    page.locator("#portLiveTab").click()
    addresses = [f"127.0.0.1:{fixture.port}" for fixture in (switch, second)]
    page.locator("#queryTargets").fill("\n".join(f"{address} 1" for address in addresses))
    page.get_by_role("button", name="포트 조회", exact=True).click()
    enter_ssh_credentials(page, switch)
    tiles = csv_view.locator("#matrix [data-port-key]")
    expect(tiles).to_have_count(2, timeout=90000)
    expect(combined).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#controlPanel")).to_be_visible()
    checkboxes = csv_view.locator("#tbody input[data-control-key]")
    expect(checkboxes).to_have_count(2)
    expect(csv_view.locator("#tbody")).to_be_visible()
    expect(page.locator("#matrixGroups")).to_be_hidden()
    expect(page.locator("#showMatrix")).to_be_hidden()
    for selector in ("#navReview", "#navCompare", "#navOverview", "#openButton", "#addButton"):
        expect(csv_view.locator(selector)).to_be_hidden()
    page.locator("#portLiveTab").click()
    expect(page.locator("#queryForm")).to_be_visible()
    expect(page.locator("#showMatrix")).to_be_hidden()
    expect(page.locator("#matrixGroups")).to_be_hidden()
    expect(page.locator("#csvViewer")).to_be_hidden()
    page.locator("#portMonitorTab").click()

    # The device selector distinguishes two switches with the same IP and hostname.
    csv_view.locator("#device").select_option(addresses[1])
    expect(checkboxes).to_have_count(1)
    page.locator("#selectionScope").select_option("switch")
    page.locator("#selectEligible").click()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    expect(checkboxes).to_be_checked()
    expect(page.locator("#controlTarget")).to_contain_text(addresses[1])
    page.locator("#clearSelection").click()
    csv_view.locator("#device").select_option("")
    expect(checkboxes).to_have_count(2)

    change_target_while_previewing(page, switch, addresses[0])

    # The two ports have the same IP, interface, hostname and AP name; only SSH ports differ.
    csv_view.locator(f'#matrix [data-port-key="{addresses[1]}/1"]').click()
    csv_view.locator("#inspectObservedPort").click()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    expect(csv_view.locator(f'#tbody tr[data-port-key="{addresses[1]}/1"] input')).to_be_checked()
    expect(
        csv_view.locator(f'#tbody tr[data-port-key="{addresses[0]}/1"] input')
    ).not_to_be_checked()
    expect(page.locator("#csvViewer")).to_be_visible()
    expect(csv_view.locator("#overviewRoot")).to_be_visible()
    page.locator("#preview").click()
    expect(page.locator("#reviewBody tbody tr")).to_have_count(1, timeout=60000)
    expect(page.locator("#reviewBody tbody tr")).to_contain_text(addresses[1])
    page.locator("#review").get_by_role("button", name="닫기").click()
    assert all("configure terminal" not in fixture.commands for fixture in (switch, second))

    page.locator("#clearSelection").click()
    expect(page.locator("#quickResult")).to_be_hidden()
    csv_view.locator("#search").fill(addresses[0])
    expect(checkboxes).to_have_count(1)
    page.locator("#selectionScope").select_option("filtered")
    page.locator("#selectVisible").click()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    expect(csv_view.locator(f'#tbody tr[data-port-key="{addresses[0]}/1"] input')).to_be_checked()
    expect(csv_view.locator("#search")).to_have_value(addresses[0])
    csv_view.locator("#search").fill("")
    second_checkbox = csv_view.locator(f'#tbody tr[data-port-key="{addresses[1]}/1"] input')
    expect(second_checkbox).not_to_be_checked()
    second_checkbox.check()
    expect(page.locator("#selectedCount")).to_have_text("선택 2개")
    expect(page.locator("#quickResult")).to_be_hidden()
    expect(page.locator("#csvViewer")).to_be_visible()
    if shots:
        page.screenshot(path=shots / "unified-port-control.png")

    assert all(fixture.poe_enabled for fixture in (switch, second))
    assert all("configure terminal" not in fixture.commands for fixture in (switch, second))
    # Deliver the mutation request, then drop just its HTTP acknowledgement.
    delivered = []
    second.off_watts = 0
    power_read = "show power-over-ethernet 1"
    off_command = "no interface 1 power-over-ethernet"
    respond = switch._respond

    def fail_after_verification(channel, command):
        # Keep the command verification successful. Reject the independent PoE query on A.
        if command == power_read and off_command in switch.commands:
            after_write = switch.commands.index(off_command) + 1
            if power_read in switch.commands[after_write:]:
                switch.commands.append(command)
                channel.sendall("\r\n".join([command, "Permission denied", switch.prompt]).encode())
                return False
        return respond(channel, command)

    def drop_submission_reply(route):
        accepted = route.fetch(max_retries=0)
        assert accepted.status == 202
        job = route.request.post_data_json["body"]["requestId"]
        assert accepted.json()["id"] == job
        delivered.append(job)
        route.abort("connectionreset")

    page.route("**/api/jobs", drop_submission_reply, times=1)
    with (
        patch.object(switch, "_respond", fail_after_verification),
        page.expect_request(
            lambda req: (
                req.method == "GET" and delivered and f"/api/jobs/{delivered[0]}?" in req.url
            )
        ),
        click_quick_poe(page, "poe_off", [(addr, "1") for addr in addresses]),
    ):
        results = page.locator("#controlResult tbody tr")
        expect(results).to_have_count(2, timeout=90000)
        assert sorted(results.locator("td:first-child").all_text_contents()) == sorted(addresses)
        assert all("검증 완료" in row for row in results.all_text_contents())
        expect_power_result(page, addresses[0], "8.1 W", "미측정")
        expect_power_result(page, addresses[1], "8.1 W", "0 W", delta="\N{MINUS SIGN}8.1 W")
    assert len(delivered) == 1
    assert all(not fixture.poe_enabled for fixture in (switch, second))
    for fixture in (switch, second):
        assert fixture.commands.count(off_command) == 1
        after_write = fixture.commands.index(off_command) + 1
        assert fixture.commands[after_write:].count(power_read) >= 2
    expect(page.locator("#csvViewer")).to_be_visible()
    expect(combined).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#selectedCount")).to_have_text("선택 0개")
    failed_row = csv_view.locator(f'#tbody tr[data-port-key="{addresses[0]}/1"]')
    ready_row = csv_view.locator(f'#tbody tr[data-port-key="{addresses[1]}/1"]')
    expect(failed_row.get_by_role("checkbox")).to_be_disabled()
    expect(failed_row).not_to_contain_text("8.1 W")
    expect(ready_row.get_by_role("checkbox")).to_be_enabled()
    expect(ready_row).to_contain_text("0 W")
    leave_failed_port_inspection_for_batch(page, csv_view, addresses[0], addresses[1])

    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if shots:
        page.screenshot(path=shots / "unified-port-mobile.png")
    page.set_viewport_size({"width": 1440, "height": 960})
    # Reload keeps the failed endpoint distinguishable from B's measured 0 W.
    page.reload()
    expect(failed_row.get_by_role("checkbox")).to_be_disabled(timeout=30000)
    expect(failed_row).not_to_contain_text("8.1 W")
    expect(ready_row).to_contain_text("0 W")
    key = addresses[0] + "/1"
    csv_view.locator(f'#matrix [data-port-key="{key}"]').click()
    untouched_commands = len(second.commands)
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/jobs")) as req:
        page.locator("#refreshPorts").click()
    assert req.value.post_data_json["body"]["targets"] == addresses[0] + " 1"
    expect(failed_row.get_by_role("checkbox")).to_be_enabled(timeout=60000)
    assert len(second.commands) == untouched_commands

    # The control verification sees 8.1 W, the first independent reading sees 0 W,
    # and the settled reading sees 9.7 W. The update must come from the latter.
    on_command = "interface 1 power-over-ethernet"
    respond_on = second._respond

    def delayed_draw(channel, command):
        if command == power_read and on_command in second.commands:
            prior = second.commands[second.commands.index(on_command) + 1 :].count(power_read)
            if prior:
                watts = 0 if prior == 1 else 9.7
                second.commands.append(command)
                lines = [
                    command,
                    "Status and Configuration Information for port 1",
                    "Power Enable : Yes",
                    "PoE Port Status : Delivering",
                    f"PD Power Draw : {watts} W",
                    second.prompt,
                ]
                channel.sendall("\r\n".join(lines).encode())
                return False
        return respond_on(channel, command)

    with patch.object(second, "_respond", delayed_draw):
        select_measured_port(page, csv_view, addresses[1], addresses[0], switch)
    assert second.commands.count(on_command) == 1
    assert second.poe_enabled and not switch.poe_enabled
    assert not page_errors, page_errors
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    run_browser_case(exercise)
    print(
        "PASS unified live matrix, filtered selection, exact endpoint binding and one-click bulk control"
    )

"""Exercise matrix selection, filtered bulk controls and exact SSH endpoints on loopback."""

import re
from threading import Event
from unittest.mock import patch

from browser_qa import click_quick_poe, run_browser_case
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


def leave_live_inspection_for_batch(page, csv_view, changed, available):
    page.locator("#portLiveTab").click()
    page.locator("#queryPorts").click()
    changed_row = csv_view.locator(f'#tbody tr[data-port-key="{changed}/1"] input')
    other_row = csv_view.locator(f'#tbody tr[data-port-key="{available}/1"] input')
    expect(changed_row).to_be_enabled(timeout=60000)
    changed_row.check()
    with click_quick_poe(page, "poe_on", [(changed, "1")]):
        result = page.locator("#controlResult tbody tr")
        expect(result).to_have_count(1, timeout=90000)
        expect(result).to_contain_text(changed)
        expect(result).to_contain_text("검증 완료")
    expect(changed_row).to_be_disabled()
    expect(other_row).to_be_enabled()

    csv_view.locator(f'#matrix [data-port-key="{changed}/1"]').click()
    csv_view.locator("#inspectObservedPort").click()
    mode = page.locator("#controlMode")
    expect(mode).to_have_value("single", timeout=60000)
    expect(mode).to_be_disabled()
    expect(page.locator("#singleIP")).to_have_value(changed)
    expect(page.locator("#singlePort")).not_to_be_editable()
    expect(page.locator("#liveScope")).to_be_hidden()

    other_row.check()
    expect(mode).to_have_value("batch")
    expect(mode).to_be_enabled()
    expect(page.locator("#liveScope")).to_be_visible()
    expect(page.locator("#controlRows")).to_be_visible()
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
    expect(combined).to_have_accessible_name("관제·비교·제어")
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
    dialog = page.get_by_role("dialog", name="포트 조회용 SSH 계정")
    dialog.locator("#queryUsername").fill(switch.username)
    dialog.locator("#queryPassword").fill(switch.password)
    dialog.get_by_role("button", name="계정 적용 후 조회").click()
    tiles = csv_view.locator("#matrix [data-port-key]")
    expect(tiles).to_have_count(2, timeout=90000)
    expect(combined).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#controlPanel")).to_be_visible()
    checkboxes = csv_view.locator("#tbody input[data-control-key]")
    expect(checkboxes).to_have_count(2)

    change_target_while_previewing(page, switch, addresses[0])

    # The two ports have the same IP, interface, hostname and AP name; only SSH ports differ.
    csv_view.locator(f'#matrix [data-port-key="{addresses[1]}/1"]').click()
    csv_view.locator("#inspectObservedPort").click()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    expect(page.locator(f'#portTable tr[data-key="{addresses[1]}|1"] input')).to_be_checked()
    expect(page.locator(f'#portTable tr[data-key="{addresses[0]}|1"] input')).not_to_be_checked()
    expect(page.locator("#csvViewer")).to_be_visible()
    expect(csv_view.locator("#overviewRoot")).to_be_visible()
    page.locator("#preview").click()
    expect(page.locator("#reviewBody tbody tr")).to_have_count(1, timeout=60000)
    expect(page.locator("#reviewBody tbody tr")).to_contain_text(addresses[1])
    page.locator("#review").get_by_role("button", name="닫기").click()
    assert all("configure terminal" not in fixture.commands for fixture in (switch, second))

    page.locator("#clearSelection").click()
    expect(page.locator("#quickResult")).to_be_hidden()
    page.locator("#returnToMonitor").click()
    expect(page.locator("#csvViewer")).to_be_focused()
    csv_view.locator("#search").fill(addresses[0])
    expect(checkboxes).to_have_count(1)
    csv_view.locator("#selectFilteredPorts").click()
    csv_view.locator("#controlObservedBatch").click()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    expect(page.locator(f'#portTable tr[data-key="{addresses[0]}|1"] input')).to_be_checked()
    expect(page.locator(f'#portTable tr[data-key="{addresses[1]}|1"] input')).not_to_be_checked()
    expect(csv_view.locator("#search")).to_have_value(addresses[0])
    csv_view.locator("#search").fill("")
    second_checkbox = csv_view.locator(f'#tbody tr[data-port-key="{addresses[1]}/1"] input')
    second_checkbox.check()
    csv_view.locator("#controlObservedBatch").click()
    expect(page.locator("#selectedCount")).to_have_text("선택 2개")
    expect(page.locator("#quickResult")).to_be_hidden()
    expect(page.locator("#csvViewer")).to_be_visible()
    if shots:
        page.screenshot(path=shots / "unified-port-control.png")

    assert all(fixture.poe_enabled for fixture in (switch, second))
    assert all("configure terminal" not in fixture.commands for fixture in (switch, second))
    # Deliver the mutation request, then drop just its HTTP acknowledgement.
    delivered = []

    def drop_submission_reply(route):
        accepted = route.fetch(max_retries=0)
        assert accepted.status == 202
        job = route.request.post_data_json["body"]["requestId"]
        assert accepted.json()["id"] == job
        delivered.append(job)
        route.abort("connectionreset")

    page.route("**/api/jobs", drop_submission_reply, times=1)
    with (
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
    assert len(delivered) == 1
    assert all(not fixture.poe_enabled for fixture in (switch, second))
    for fixture in (switch, second):
        assert fixture.commands.count("no interface 1 power-over-ethernet") == 1
    expect(page.locator("#csvViewer")).to_be_visible()
    expect(combined).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#selectedCount")).to_have_text("선택 0개")
    expect(checkboxes.nth(0)).to_be_disabled()
    expect(checkboxes.nth(1)).to_be_disabled()
    for tile in tiles.all():
        expect(tile).to_have_attribute("aria-label", re.compile("현재 상태 재조회 필요"))
    tiles.first.click()
    expect(csv_view.locator("#detail [data-freshness-key]")).to_be_visible()
    expect(csv_view.locator("#detail")).to_contain_text("현재 상태 재조회 필요")

    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if shots:
        page.screenshot(path=shots / "unified-port-mobile.png")
    page.set_viewport_size({"width": 1440, "height": 960})
    leave_live_inspection_for_batch(page, csv_view, addresses[1], addresses[0])
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

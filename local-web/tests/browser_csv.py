"""Verify the CSV viewer starts empty and shows only the selected query exports."""

from threading import Event
from unittest.mock import patch
from urllib.parse import urlsplit

from browser_qa import enter_ssh_credentials, expect_power_result, run_browser_case
from playwright.sync_api import expect
from verify_http_ssh import Http


def assert_empty(view, *, standalone=True):
    expect(view.locator("#sourceName")).to_have_text(
        "CSV 미선택" if standalone else "조회 결과 대기"
    )
    if standalone:
        expect(view.locator("#openButton")).to_be_visible()
        expect(view.locator("#addButton")).to_be_disabled()
        expect(view.locator("#navReview")).to_be_disabled()
    else:
        for selector in ("#openButton", "#addButton", "#navOverview", "#navReview", "#navCompare"):
            expect(view.locator(selector)).to_be_hidden()
    expect(view.locator("#overviewRoot")).to_be_hidden()
    expect(view.locator("#matrix [data-port-key]")).to_have_count(0)
    expect(view.locator("#tbody tr")).to_have_count(0)
    expect(view.locator("#extraView")).to_be_hidden()


def exports_from_switch(http, switch):
    address = f"127.0.0.1:{switch.port}"
    http.job("session.open", username=switch.username, password=switch.password)
    key = http.job("hostkey.inspect", targets=address)["checks"][0]
    http.job("hostkey.approve", endpoint=address, fingerprint=key["fingerprint"])
    exports = []
    for name, power in (("이전 관측.csv", True), ("다음 관측.csv", False)):
        switch.poe_enabled = power
        result = http.job("ports.query", targets=address + " 1")
        (row,) = result["rows"]
        assert row["status"] == "ok" and row["link_status"] == ("Up" if power else "Down")
        content = http.download(result["files"][0])
        exports.append({"name": name, "mimeType": "text/csv", "buffer": content})
    return exports


def assert_comparison(view, earlier, later):
    expect(view.locator("#pageTitle")).to_have_text("포트별 변화 비교")
    expect(view.locator("#sourceName")).to_have_text(later["name"])
    expect(view.locator(".dr-src strong")).to_have_text([earlier["name"], later["name"]])
    expect(view.locator(".dr-list button")).to_have_count(1)
    link = view.locator(".dr-diff tr").filter(
        has=view.get_by_role("rowheader", name="링크", exact=True)
    )
    expect(link.locator("td")).to_have_text(["Up", "Down"])


def assert_historical_context(page, view):
    expect(page.locator("#queryForm")).to_be_visible()
    expect(page.locator("#csvViewer")).to_be_visible()

    expect(page.locator("#controlPanel")).to_be_hidden()
    expect(page.locator("#controlSourceHint")).to_be_visible()
    expect(view.locator("#controlObservedBatch")).to_be_hidden()
    expect(view.locator("#tbody input[data-control-key]:visible")).to_have_count(0)
    expect(page.locator("#quickOff")).to_be_hidden()


def open_comparison_during_query(page, switch, view, earlier, later):
    reading, resume = Event(), Event()
    respond = switch._respond

    def wait_for_csv(channel, command):
        if command == "show power-over-ethernet 1":
            reading.set()
            assert resume.wait(30), (
                "Browser did not finish opening comparison while query was active"
            )
        return respond(channel, command)

    with patch.object(switch, "_respond", wait_for_csv):
        try:
            page.locator("#queryPorts").click()
            assert reading.wait(30), "Live query did not reach the selected switch port"
            with page.expect_file_chooser() as picker:
                page.locator("#openPortCsv").click()
            picker.value.set_files([earlier, later])
            assert_comparison(view, earlier, later)
            assert_historical_context(page, view)
        finally:
            resume.set()
        expect(page.locator("#statusText")).to_contain_text("포트 조회 완료", timeout=60000)
    expect(page.locator("#portCompareTab")).to_have_attribute("aria-pressed", "true")
    assert_comparison(view, earlier, later)
    assert_historical_context(page, view)
    page.get_by_role("button", name="최근 실시간 조회 보기", exact=True).click()


def exercise_csv(page, url, switch, _second_switch, _shots, _backup_directory):
    page.goto(url)
    expect(page.locator("#inspectSingle")).to_have_count(1)
    page.get_by_role("link", name="관제·비교·제어", exact=True).click()
    embedded = page.frame_locator("#csvViewer")
    assert_empty(embedded, standalone=False)
    expect(page.locator("#portReviewTab")).to_be_disabled()
    page.evaluate("location.hash = 'csv'")
    expect(page.locator('[data-page="ports"]')).to_be_visible()
    expect(page.locator("#csvViewer")).to_be_visible()
    parsed = urlsplit(url)
    direct = page.context.browser.new_page(storage_state=page.context.storage_state())
    direct.goto(f"{parsed.scheme}://{parsed.netloc}/csv")
    assert_empty(direct)

    invalid = {"name": "잘못된.csv", "mimeType": "text/csv", "buffer": b"other,columns\r\na,b\r\n"}
    with direct.expect_file_chooser() as selection:
        direct.locator("#openButton").click()
    selection.value.set_files(invalid)
    expect(direct.locator("#fileError")).to_contain_text(invalid["name"])
    assert_empty(direct)

    http = Http(parsed.port, parsed.path.rsplit("/", 1)[-1])
    earlier, later = exports_from_switch(http, switch)
    http.job("session.close")
    direct.locator("#openCsv").set_input_files(earlier)
    expect(direct.locator("#sourceName")).to_have_text(earlier["name"])
    expect(direct.locator("#fileError")).to_be_hidden()
    expect(direct.locator("#overviewRoot")).to_be_visible()
    expect(direct.locator("#matrix [data-port-key]")).to_have_attribute("data-state", "normal")
    expect(direct.locator("#tbody tr")).to_have_count(1)
    expect(direct.locator("#tbody tr")).to_contain_text("fixture-ap")
    direct.locator("#matrix [data-port-key]").click()
    expect(direct.locator("#inspectObservedPort")).to_have_count(0)
    expect(direct.locator("#tbody input[data-control-key]")).to_have_count(0)
    direct.locator("#priorityButton").click()
    expect(direct.locator("#extraView")).to_be_visible()
    expect(direct.locator("#extraView")).to_contain_text("점검 목록")
    direct.locator("#navOverview").click()
    direct.locator("#addCsv").set_input_files(invalid)
    expect(direct.locator("#fileError")).to_contain_text(invalid["name"])
    expect(direct.locator("#sourceName")).to_have_text(earlier["name"])
    expect(direct.locator("#tbody tr")).to_contain_text("fixture-ap")
    direct.locator("#addCsv").set_input_files(later)
    assert_comparison(direct, earlier, later)
    expect(direct.locator("#compareObservedPort")).to_have_count(0)

    # A registered but unqueried port and a pair of CSVs are distinct data sources.
    # Importing history must leave the pending target intact without changing its power state.
    start = len(switch.commands)
    address = f"127.0.0.1:{switch.port}"
    page.locator("#queryTargets").fill(address + " 2")
    page.locator("#targetOptions > summary").click()
    page.locator("#targetVerify").click()
    page.locator("#acceptTargets").click()
    page.locator("#targetOptions > summary").click()
    pending = page.locator(f'.matrix-tile[data-key="{address}|2"]')
    expect(pending).to_be_visible()
    expect(pending).to_have_attribute("data-phase", "pending")
    expect(page.locator("#csvViewer")).to_be_hidden()

    with page.expect_file_chooser() as parent_selection:
        page.locator("#openPortCsv").click()
    parent_selection.value.set_files([later, earlier])
    assert_comparison(embedded, earlier, later)
    expect(page.locator("#portCompareTab")).to_have_attribute("aria-pressed", "true")
    assert_historical_context(page, embedded)
    # Typing another target drafts the next query while the displayed snapshots stay fixed.
    draft = page.locator("#queryTargets")
    saved_targets = draft.input_value()
    draft.fill(address + " 3")
    assert_comparison(embedded, earlier, later)
    assert_historical_context(page, embedded)
    switch_source = page.locator("#showMatrix")
    expect(switch_source).to_have_accessible_name("등록 포트 보기")
    switch_source.click()
    expect(pending).to_be_visible()
    expect(pending).to_have_attribute("data-state", "unclassified")
    expect(page.locator(".matrix-tile")).to_have_count(1)
    expect(draft).to_have_value(address + " 3")
    draft.fill(saved_targets)
    expect(page.locator("#csvViewer")).to_be_hidden()
    expect(switch_source).to_have_accessible_name("열어 둔 CSV 보기")
    switch_source.click()
    expect(pending).to_be_hidden()
    assert_comparison(embedded, earlier, later)
    assert_historical_context(page, embedded)
    assert len(switch.commands) == start
    embedded.locator("#openCsv").set_input_files([later, invalid])
    expect(embedded.locator("#fileError")).to_contain_text(invalid["name"])
    assert_comparison(embedded, earlier, later)
    page.locator('nav a[href="#ports"]').click()
    embedded.locator("#openCsv").set_input_files(earlier)
    expect(embedded.locator("#sourceName")).to_have_text(earlier["name"])
    expect(embedded.locator("#tbody")).to_contain_text("8.1 W")
    expect(page.locator("#portMonitorTab")).to_have_attribute("aria-pressed", "true")
    page.locator("#portReviewTab").click()
    expect(embedded.locator("#extraView")).to_contain_text("점검 목록")
    expect(page.locator("#portReviewTab")).to_have_attribute("aria-pressed", "true")
    page.locator("#portMonitorTab").click()
    assert_historical_context(page, embedded)
    embedded.locator("#matrix [data-port-key]").click()
    command_count = len(switch.commands)
    embedded.locator("#inspectObservedPort").click()
    enter_ssh_credentials(page, switch)
    expect(page.locator("#singleControls")).to_be_visible(timeout=60000)
    expect(page.locator("#singleIP")).to_have_value(f"127.0.0.1:{switch.port}")
    expect(page.locator("#singlePort")).to_have_value("1")
    expect(page.locator("#singleControls + .output")).to_contain_text("PoE OFF")
    expect(page.locator("#csvViewer")).to_be_visible()
    expect(embedded.locator("#sourceName")).to_have_text(earlier["name"])
    expect(embedded.locator("#tbody")).to_contain_text("8.1 W")
    expect(page.locator("#singleIP")).not_to_be_editable()
    expect(page.locator("#singlePort")).not_to_be_editable()
    expect(page.locator("#controlMode")).to_be_disabled()
    page.locator("#operation").select_option("poe_on")
    page.locator("#preview").click()
    expect(page.locator("#reviewBody")).to_contain_text(
        "interface 1 power-over-ethernet", timeout=60000
    )
    expect(page.get_by_role("button", name="승인하고 명령 실행")).to_be_disabled()
    page.locator("#review").get_by_role("button", name="닫기").click()
    assert not switch.poe_enabled
    inspected = switch.commands[command_count:]
    assert "show power-over-ethernet 1" in inspected and "configure terminal" not in inspected
    page.locator("#queryTargets").fill(f"127.0.0.1:{switch.port} 1")
    open_comparison_during_query(page, switch, embedded, earlier, later)
    expect(embedded.locator("#sourceName")).to_have_text("실시간 조회 결과.csv", timeout=60000)
    expect(embedded.locator("#overviewRoot")).to_be_visible()
    # A disabled port without a measured wattage is a neutral cell.
    expect(embedded.locator("#matrix [data-port-key]")).to_have_attribute(
        "data-state", "unclassified"
    )
    with page.expect_file_chooser() as compare_file:
        page.locator("#portCompareTab").click()
    compare_file.value.set_files(earlier)
    assert_comparison(embedded, earlier, {"name": "실시간 조회 결과.csv"})
    assert_historical_context(page, embedded)
    baseline_commands = len(switch.commands)
    embedded.locator("#compareObservedPort").click()
    expect(page.locator("#singleControls + .output")).to_contain_text("PoE OFF", timeout=60000)
    expect(page.locator("#csvViewer")).to_be_visible()
    assert_comparison(embedded, earlier, {"name": "실시간 조회 결과.csv"})
    assert "show power-over-ethernet 1" in switch.commands[baseline_commands:]
    assert "configure terminal" not in switch.commands[baseline_commands:]
    page.locator("#operation").select_option("poe_on")
    page.locator("#preview").click()
    expect(page.locator("#review")).to_be_visible(timeout=60000)
    page.locator("#reviewBody input").fill(page.locator("#reviewBody code").inner_text())
    page.get_by_role("button", name="승인하고 명령 실행").click()
    expect_power_result(page, address, "미측정", "8.1 W")
    assert switch.commands.count("interface 1 power-over-ethernet") == 1
    # Control data updates, while the chosen before/after snapshots retain their original values.
    expect(page.locator("#portCompareTab")).to_have_attribute("aria-pressed", "true")
    assert_comparison(embedded, earlier, {"name": "실시간 조회 결과.csv"})
    embedded.locator("#openCsv").set_input_files(later)
    expect(embedded.locator("#sourceName")).to_have_text(later["name"])
    assert_historical_context(page, embedded)
    direct.close()
    http.post("/api/shutdown", {})


if __name__ == "__main__":
    run_browser_case(exercise_csv)
    print(
        "PASS Chrome CSV viewer: empty standalone and embedded views, query export, review, "
        "two-file selection and append, live versus baseline, retained comparison after PoE control, "
        "and invalid-file recovery."
    )

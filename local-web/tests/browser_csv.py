"""Verify the CSV viewer starts empty and shows only the selected query exports."""

from urllib.parse import urlsplit

from browser_qa import run_browser_case
from playwright.sync_api import expect
from verify_http_ssh import Http


def assert_empty(view):
    expect(view.locator("#sourceName")).to_have_text("CSV 미선택")
    expect(view.locator("#openButton")).to_be_enabled()
    expect(view.locator("#addButton")).to_be_disabled()
    expect(view.locator("#navReview")).to_be_disabled()
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


def exercise_csv(page, url, switch, _second_switch, _shots, _backup_directory):
    page.goto(url)
    expect(page.locator("#inspectSingle")).to_have_count(1)
    page.get_by_role("link", name="포트 관제·제어").click()
    embedded = page.frame_locator("#csvViewer")
    assert_empty(embedded)
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
    direct.locator("#openCsv").set_input_files(earlier)
    expect(direct.locator("#sourceName")).to_have_text(earlier["name"])
    expect(direct.locator("#fileError")).to_be_hidden()
    expect(direct.locator("#overviewRoot")).to_be_visible()
    expect(direct.locator("#matrix [data-port-key]")).to_have_count(1)
    expect(direct.locator("#tbody tr")).to_have_count(1)
    expect(direct.locator("#tbody tr")).to_contain_text("fixture-ap")
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

    with page.expect_file_chooser() as parent_selection:
        page.locator("#openPortCsv").click()
    parent_selection.value.set_files([later, earlier])
    assert_comparison(embedded, earlier, later)
    expect(page.locator("#portControlTab")).to_be_disabled()
    embedded.locator("#openCsv").set_input_files([later, invalid])
    expect(embedded.locator("#fileError")).to_contain_text(invalid["name"])
    assert_comparison(embedded, earlier, later)
    page.locator('nav a[href="#connect"]').click()
    page.locator("#username").fill(switch.username)
    page.locator("#password").fill(switch.password)
    page.locator("#loginForm button.primary").click()
    expect(page.locator("#connectionState")).to_contain_text(switch.username)
    page.locator('nav a[href="#ports"]').click()
    embedded.locator("#openCsv").set_input_files(earlier)
    expect(embedded.locator("#sourceName")).to_have_text(earlier["name"])
    expect(embedded.locator("#tbody")).to_contain_text("8.1 W")
    expect(page.locator("#portControlTab")).to_be_disabled()
    embedded.locator("#matrix [data-port-key]").click()
    command_count = len(switch.commands)
    embedded.locator("#inspectObservedPort").click()
    expect(page.locator("#singleControls")).to_be_visible(timeout=60000)
    expect(page.locator("#singleIP")).to_have_value(f"127.0.0.1:{switch.port}")
    expect(page.locator("#singlePort")).to_have_value("1")
    expect(page.locator("#singleControls + .output")).to_contain_text("PoE OFF")
    inspected = switch.commands[command_count:]
    assert "show power-over-ethernet 1" in inspected and "configure terminal" not in inspected
    page.locator("#portLiveTab").click()
    page.locator("#queryTargets").fill(f"127.0.0.1:{switch.port} 1")
    page.locator("#queryForm .primary").click()
    expect(embedded.locator("#sourceName")).to_have_text("실시간 조회 결과.csv", timeout=60000)
    expect(embedded.locator("#overviewRoot")).to_be_visible()
    embedded.locator("#addCsv").set_input_files(earlier)
    assert_comparison(embedded, earlier, {"name": "실시간 조회 결과.csv"})
    expect(page.locator("#portControlTab")).to_be_disabled()
    direct.close()
    http.post("/api/shutdown", {})


if __name__ == "__main__":
    run_browser_case(exercise_csv)
    print(
        "PASS Chrome CSV viewer: empty standalone and embedded views, query export, review, "
        "two-file selection and append, live versus baseline, historical port requery without writes, "
        "and invalid-file recovery."
    )

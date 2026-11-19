"""Verify full-query, view-switch and control scope behavior in a real browser on loopback."""

import sys

from browser_power_matrix import PowerMatrixSwitch
from browser_qa import enter_ssh_credentials, open_port_controls, run_browser_case
from browser_unified_controls import resize_workspace
from playwright.sync_api import expect

HEADINGS = ["포트 번호", "전력 판정", "실측 W", "LLDP AP 이름"]
SIZES = ((1366, 768), (390, 844), (320, 568))


def assert_presentation(page, frame, presentation):
    for mode, panel in (("matrix", "#matrix"), ("list", ".resulttable")):
        active = mode == presentation
        expect(page.locator(f"#{mode}View")).to_have_attribute("aria-pressed", str(active).lower())
        if active:
            expect(frame.locator(panel)).to_be_visible()
        else:
            expect(frame.locator(panel)).to_be_hidden()


def assert_table_fits(page, frame):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for selector in (".tablewrap", "html"):
        assert frame.locator(selector).evaluate("e => e.scrollWidth <= e.clientWidth + 1"), selector
    assert frame.locator(".resulttable th[scope=col]").all_text_contents() == HEADINGS
    assert frame.locator("#tbody tr[data-port-key]").evaluate_all(
        "rows => rows.length > 0 && rows.every(r => r.cells.length === 4)"
    )


def exercise(page, url, switch, second, shots, _backup):
    addresses = [f"127.0.0.1:{fixture.port}" for fixture in (switch, second)]
    first_key, other_key = addresses[0] + "/3", addresses[1] + "/1"
    page.goto(url)
    page.locator('nav a[href="#ports"]').click()
    matrix_button, list_button = page.locator("#matrixView"), page.locator("#listView")
    expect(list_button).to_be_hidden()
    page.locator("#queryTargets").fill(f"{addresses[0]} 1-5\n{addresses[1]} 1")
    page.locator("#queryPorts").click()
    enter_ssh_credentials(page, switch)
    frame = page.frame_locator("#matrixViewer")
    tiles = frame.locator("#matrix [data-port-key]")
    rows = frame.locator("#tbody tr[data-port-key]")
    expect(tiles).to_have_count(6, timeout=90000)
    expect(page.locator("#queryPorts")).to_be_enabled()
    assert_presentation(page, frame, "matrix")
    before = [len(fixture.commands) for fixture in (switch, second)]
    first_tile = frame.locator(f'#matrix [data-port-key="{first_key}"]')
    other_tile = frame.locator(f'#matrix [data-port-key="{other_key}"]')
    first_row = frame.locator(f'#tbody tr[data-port-key="{first_key}"]')
    other_row = frame.locator(f'#tbody tr[data-port-key="{other_key}"]')
    first_tile.click()

    for width, height in SIZES:
        resize_workspace(page, width, height)
        if shots:
            page.screenshot(path=shots / f"port-matrix-{width}.png")
        list_button.click()
        assert_presentation(page, frame, "list")
        assert_table_fits(page, frame)
        expect(rows).to_have_count(6)
        groups = frame.locator("#tbody tr.result-group th[colspan='4']")
        expect(groups).to_have_count(2)
        for address in addresses:
            expect(groups.filter(has_text=address)).to_contain_text("edge-lab")
        expect(first_row).to_have_class("selected")
        expect(first_row.locator("button.row-select")).to_have_text("3")
        expect(first_row.locator("td").nth(2)).to_contain_text("8 W")
        expect(first_row.locator("td").nth(3)).to_have_text("matrix-ap-3")
        if shots:
            page.screenshot(path=shots / f"port-list-{width}.png")
        other_row.locator("button.row-select").click()
        expect(page.locator("#controlTarget")).to_contain_text(addresses[1])
        matrix_button.click()
        assert_presentation(page, frame, "matrix")
        expect(other_tile).to_have_attribute("aria-pressed", "true")
        first_tile.click()

    resize_workspace(page, 1366, 768)
    frame.locator("#search").fill("matrix-ap-3")
    frame.locator("#device").select_option(addresses[0])
    frame.locator("#status").select_option("normal")
    for mode, button in (("list", list_button), ("matrix", matrix_button), ("list", list_button)):
        button.click()
        assert_presentation(page, frame, mode)
        expect(frame.locator("#search")).to_have_value("matrix-ap-3")
        expect(frame.locator("#device")).to_have_value(addresses[0])
        expect(frame.locator("#status")).to_have_value("normal")
        expect(rows if mode == "list" else tiles).to_have_count(1)
        if mode == "list":
            expect(first_row).to_have_class("selected")
        else:
            expect(first_tile).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#controlTarget")).to_contain_text(addresses[0])

    frame.locator("#search").fill("")
    frame.locator("#device").select_option("")
    frame.locator("#status").select_option("all")
    first_row.locator("input").check()
    other_row.locator("input").check()
    matrix_button.click()
    expect(frame.locator("#matrix .for-action")).to_have_count(2)
    expect(page.locator("#selectedCount")).to_have_text("선택 2개")
    list_button.click()
    expect(first_row.locator("input")).to_be_checked()
    expect(other_row.locator("input")).to_be_checked()
    expect(page.locator("#selectedCount")).to_have_text("선택 2개")
    open_port_controls(page)
    page.locator("#clearSelection").click()
    page.keyboard.press("Escape")

    changes = page.locator("#portChanges")
    changes.click()
    expect(rows).to_have_count(0)
    matrix_button.click()
    expect(changes).to_have_attribute("aria-pressed", "true")
    expect(tiles).to_have_count(0)
    list_button.click()
    expect(rows).to_have_count(0)
    changes.click()
    expect(rows).to_have_count(6)
    review = page.locator("#portReviewTab")
    review.click()
    expect(frame.locator("#extraView")).to_be_visible()
    matrix_button.click()
    expect(review).to_have_attribute("aria-pressed", "true")
    expect(frame.locator("#extraView")).to_be_visible()
    list_button.click()
    expect(review).to_have_attribute("aria-pressed", "true")
    page.locator("#portMonitorTab").click()
    assert [len(fixture.commands) for fixture in (switch, second)] == before

    other_row.locator(".row-select").click()
    open_port_controls(page)
    page.locator("#refreshPorts").click()
    expect(page.locator("#statusText")).to_contain_text("재조회 완료", timeout=90000)
    assert_presentation(page, frame, "list")
    with page.expect_request(
        lambda request: request.url.endswith("/api/jobs") and request.method == "POST"
    ):
        page.locator("#queryPorts").click()
    expect(page.locator("#statusText")).to_contain_text("포트 조회 완료", timeout=90000)
    assert_presentation(page, frame, "matrix")
    expect(tiles).to_have_count(6)
    assert all("configure terminal" not in fixture.commands for fixture in (switch, second))
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    engine = sys.argv[1] if len(sys.argv) > 1 else "chromium"
    run_browser_case(exercise, switch_factory=PowerMatrixSwitch, browser_engine=engine)
    print(
        f"PASS {engine}: matrix/list, four columns, endpoint grouping, filters, selection and query scope."
    )

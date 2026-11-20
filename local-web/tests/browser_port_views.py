"""Verify full-query, view-switch and control scope behavior in a real browser on loopback."""

import re
import sys

from browser_power_matrix import PowerMatrixSwitch
from browser_qa import (
    enter_ssh_credentials,
    expect_hidden_selection,
    open_port_controls,
    power_filter,
    run_browser_case,
)
from browser_unified_controls import hit_point, resize_workspace
from playwright.sync_api import expect

HEADINGS = ["포트 번호", "전력 판정", "실측 W", "LLDP AP 이름"]
SIZES = ((1366, 768), (390, 844), (320, 568))
LONG_AP = "engineering-floor-3-east-wing-access-point-0002"


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
    headings = frame.locator(".resulttable th[scope=col]")
    assert headings.all_text_contents() == HEADINGS
    for heading in headings.all():
        expect(heading).to_be_in_viewport(ratio=1)
        hit_point(heading)
    assert frame.locator("#tbody tr[data-port-key]").evaluate_all(
        "rows => rows.length > 0 && rows.every(r => r.cells.length === 4)"
    )


def exercise(page, url, switch, second, shots, _backup):
    switch.ap_names["2"] = LONG_AP
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
    expect(frame.get_by_role("button", name="여러 포트 선택", exact=True)).to_have_count(0)
    select_all = page.locator("#matrixSelectAll")
    expect(select_all).to_have_accessible_name("전체 선택")
    before = [len(fixture.commands) for fixture in (switch, second)]
    first_tile = frame.locator(f'#matrix [data-port-key="{first_key}"]')
    other_tile = frame.locator(f'#matrix [data-port-key="{other_key}"]')
    first_row = frame.locator(f'#tbody tr[data-port-key="{first_key}"]')
    other_row = frame.locator(f'#tbody tr[data-port-key="{other_key}"]')
    first_group = frame.get_by_role("group", name=re.compile(re.escape(addresses[0])))
    for port, name in (
        ("1", "matrix-ap-1"),
        ("2", LONG_AP),
        ("3", "matrix-ap-3"),
        ("5", "matrix-ap-5"),
    ):
        tile = first_group.locator(f'[data-port-key="{addresses[0]}/{port}"]')
        expect(tile.locator(".tile-ap-name")).to_be_visible()
        expect(tile.locator(".tile-ap-name")).to_have_text(name)
        expect(tile).to_have_accessible_name(re.compile(re.escape(name)))
    expect(first_group.locator('[data-port-key$="/4"] .tile-ap-name')).to_have_count(0)
    expect(other_tile.locator(".tile-ap-name")).to_have_text("fixture-ap")
    long_tile = first_group.locator('[data-port-key$="/2"]')
    resize_workspace(page, 320, 568)
    if page.context.browser.browser_type.name == "webkit":
        long_tile.tap()
    else:
        long_tile.focus()
        long_tile.press("Enter")
    expect(frame.locator("#detail .ap-name")).to_contain_text(LONG_AP)
    long_tile.click()
    expect(long_tile).to_have_attribute("aria-pressed", "false")
    first_tile.click()
    expect(first_row.locator("input")).to_be_checked()

    for width, height in SIZES:
        resize_workspace(page, width, height)
        if shots:
            page.screenshot(path=shots / f"port-matrix-{width}.png")
        list_button.click()
        assert_presentation(page, frame, "list")
        expect(select_all).to_be_visible()
        expect(frame.locator("#tableCount")).to_have_count(0)
        expect(frame.locator("#tableRange")).to_contain_text("6")
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
        expect(other_row).to_have_class("selected")
        expect(page.locator("#controlTarget")).to_contain_text(addresses[0])
        matrix_button.click()
        assert_presentation(page, frame, "matrix")
        expect(other_tile).to_have_attribute("aria-pressed", "false")
        expect(first_tile).to_have_attribute("aria-pressed", "true")
        first_tile.click()
        expect(page.locator("#quickActions")).to_be_hidden()
        first_tile.click()
        expect(first_row.locator("input")).to_be_checked()

    resize_workspace(page, 1366, 768)
    frame.locator("#search").fill("matrix-ap-3")
    frame.locator("#device").select_option(addresses[0])
    normal = power_filter(frame, "normal")
    normal.click()
    for mode, button in (("list", list_button), ("matrix", matrix_button), ("list", list_button)):
        button.click()
        assert_presentation(page, frame, mode)
        expect(frame.locator("#search")).to_have_value("matrix-ap-3")
        expect(frame.locator("#device")).to_have_value(addresses[0])
        expect(normal).to_have_attribute("aria-pressed", "true")
        expect(rows if mode == "list" else tiles).to_have_count(1)
        if mode == "list":
            expect(first_row).to_have_class("selected")
        else:
            expect(first_tile).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#controlTarget")).to_contain_text(addresses[0])

    frame.locator("#search").fill("")
    frame.locator("#device").select_option("")
    normal.click()
    first_row.locator("input").check()
    other_row.locator("input").check()
    matrix_button.click()
    expect(frame.locator("#matrix .for-action")).to_have_count(2)
    list_button.click()
    other_row.locator(".row-select").click()
    expect(other_row).to_have_class("selected")
    other_row.locator("input").uncheck()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    matrix_button.click()
    expect(frame.locator("#matrix .for-action")).to_have_count(1)
    if page.context.browser.browser_type.name == "webkit":
        resize_workspace(page, 390, 844)
        other_tile.tap()
    else:
        other_tile.focus()
        other_tile.press("Space")
    expect(page.locator("#selectedCount")).to_have_text("선택 2개")
    list_button.click()
    expect(first_row.locator("input")).to_be_checked()
    expect(other_row.locator("input")).to_be_checked()
    expect(page.locator("#selectedCount")).to_have_text("선택 2개")
    resize_workspace(page, 1366, 768)
    select_all.click()
    expect(select_all).to_have_accessible_name("전체 선택 해제")
    select_all.click()
    expect(page.locator("#selectedCount")).to_have_text("선택 0개")

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
    red = power_filter(frame, "unpowered")
    amber = power_filter(frame, "limited")
    red.click()
    amber.click()
    expect(rows).to_have_count(3)
    matrix_button.click()
    expect(tiles).to_have_count(3)
    expect(red).to_have_attribute("aria-pressed", "true")
    list_button.click()
    expect(amber).to_have_attribute("aria-pressed", "true")
    red.click()
    expect(rows).to_have_count(2)
    amber.click()
    expect(rows).to_have_count(6)
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
    check_paged_select_all(page, frame, switch, second, addresses, shots)
    assert all("configure terminal" not in fixture.commands for fixture in (switch, second))
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


def check_paged_select_all(page, frame, switch, second, addresses, shots):
    switch.draws.update({str(port): 8 for port in range(6, 49)})
    page.locator("#editQueryTargets").click()
    page.locator("#queryTargets").fill(f"{addresses[0]} 1-48\n{addresses[1]} 1")
    page.locator("#queryPorts").click()
    tiles = frame.locator("#matrix [data-port-key]")
    expect(tiles).to_have_count(49, timeout=120000)
    expect(page.locator("#queryPorts")).to_be_enabled()
    baseline = [len(peer.commands) for peer in (switch, second)]
    toggle = page.locator("#matrixSelectAll")
    device = frame.locator("#device")
    search = frame.locator("#search")
    count = page.locator("#selectedCount")
    other = frame.locator(f'#matrix [data-port-key="{addresses[1]}/1"]')
    first = frame.locator(f'#matrix [data-port-key="{addresses[0]}/1"]')

    group = frame.get_by_role("group", name=re.compile(re.escape(addresses[0])))
    for width, height in SIZES:
        resize_workspace(page, width, height)
        rows = group.locator("[data-port-key]").evaluate_all(
            """tiles => {
                const rows = new Map();
                for (const tile of tiles) {
                    rows.set(tile.offsetTop, (rows.get(tile.offsetTop) || 0) + 1);
                }
                return [...rows.values()];
            }"""
        )
        columns = max(rows)
        assert sum(rows) == 48 and columns >= (6 if width > 400 else 2), (width, rows)
        assert group.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        assert frame.locator("html").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        if shots:
            print(
                f"48-port matrix at {width} px: up to {columns} columns, {len(rows)} rows, no horizontal overflow",
                flush=True,
            )
            page.screenshot(path=shots / f"port-ap-name-48-{width}.png")
    resize_workspace(page, 1366, 768)
    # Inspecting a row offers it as the sole control target even with no checkboxes checked.
    page.locator("#listView").click()
    rows = frame.locator("#tbody tr[data-port-key]")
    inspected = rows.first
    inspected_key = inspected.get_attribute("data-port-key")
    inspected.locator(".row-select").click()
    expect(inspected.get_by_role("checkbox")).not_to_be_checked()
    expect(count).to_have_text("선택 1개")
    expect_hidden_selection(page, 0)
    frame.locator("#nextPage").click()
    expect(frame.locator(f'#tbody tr[data-port-key="{inspected_key}"]')).to_have_count(0)
    expect(count).to_have_text("선택 1개")
    expect_hidden_selection(page, 1)
    frame.locator("#prevPage").click()
    expect_hidden_selection(page, 0)
    inspected.get_by_role("checkbox").check()
    inspected.get_by_role("checkbox").uncheck()
    expect(count).to_have_text("선택 0개")
    expect_hidden_selection(page, 0)
    page.locator("#matrixView").click()

    # Port 1 on the second endpoint must survive an all/none round trip on the first.
    other.click()
    device.select_option(addresses[0])
    expect_hidden_selection(page, 1)
    first.click()
    expect(count).to_have_text("선택 2개")
    expect(toggle).to_have_accessible_name("전체 선택")
    page.locator("#listView").click()
    expect(rows).to_have_count(18)
    eligible = frame.locator("#matrix [data-port-key][aria-pressed=true]")
    if page.context.browser.browser_type.name == "webkit":
        resize_workspace(page, 320, 568)
        hit_point(toggle)
        toggle.tap()
    else:
        toggle.focus()
        toggle.press("Enter")
    expect(eligible).to_have_count(48)
    expect(count).to_have_text("선택 49개")
    expect_hidden_selection(page, 31)
    expect(toggle).to_have_accessible_name("전체 선택 해제")
    frame.locator("#nextPage").click()
    expect(rows).to_have_count(18)
    frame.locator("#nextPage").click()
    expect(rows).to_have_count(12)
    expect_hidden_selection(page, 37)
    expect(frame.locator("#tbody input:checked")).to_have_count(12)
    last = frame.locator(f'#tbody tr[data-port-key="{addresses[0]}/48"] input')
    last.uncheck()
    expect(count).to_have_text("선택 48개")
    expect_hidden_selection(page, 37)
    expect(toggle).to_have_accessible_name("전체 선택")
    last.check()
    expect(toggle).to_have_accessible_name("전체 선택 해제")
    toggle.click()
    expect(count).to_have_text("선택 1개")
    expect_hidden_selection(page, 1)
    expect(toggle).to_have_accessible_name("전체 선택")
    expect(eligible).to_have_count(0)
    resize_workspace(page, 1366, 768)
    device.select_option("")
    page.locator("#matrixView").click()
    expect(other).to_have_attribute("aria-pressed", "true")
    expect_hidden_selection(page, 0)
    expect(first).to_have_attribute("aria-pressed", "false")
    search.fill("no_matching_switch_or_port")
    expect(tiles).to_have_count(0)
    expect(toggle).to_be_disabled()
    expect_hidden_selection(page, 1)
    search.fill("")
    expect_hidden_selection(page, 0)
    other.click()
    expect(count).to_have_text("선택 0개")
    expect(page.locator("#quickActions")).to_be_hidden()
    assert [len(peer.commands) for peer in (switch, second)] == baseline


if __name__ == "__main__":
    engine = sys.argv[1] if len(sys.argv) > 1 else "chromium"
    run_browser_case(
        exercise, switch_factory=PowerMatrixSwitch, browser_engine=engine, touch=engine == "webkit"
    )
    print(
        f"PASS {engine}: direct port selection, scoped select-all across pages, matrix/list and SSH endpoints."
    )

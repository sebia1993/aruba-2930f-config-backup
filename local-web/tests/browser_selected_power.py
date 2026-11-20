"""Exercise the selection-only power toolbar in both port views and on touch screens."""

import sys

from browser_qa import (
    click_quick_poe,
    enter_ssh_credentials,
    expect_hidden_selection,
    expect_no_quick_power,
    expect_power_result,
    open_target_editor,
    power_filter,
    quick_power_group,
    run_browser_case,
)
from browser_unified_controls import hit_point, resize_workspace, verify_quick_power_buttons
from playwright.sync_api import expect


def verify_placement(page, other_document):
    # There is only one actionable toolbar, even when switching documents.
    active = quick_power_group(page)
    expect(active).to_be_visible()
    expect(other_document.locator("#quickActions:visible")).to_have_count(0)
    verify_quick_power_buttons(page)
    anchor = active.locator("xpath=preceding-sibling::*[1]")
    last = active.locator("#quickOn").bounding_box()
    first = active.locator("#quickOff").bounding_box()
    badge = anchor.bounding_box()
    width = page.viewport_size["width"]
    right = first["x"] >= badge["x"] + badge["width"] - 1
    inline = right and first["y"] < badge["y"] + badge["height"]
    assert inline and first["x"] - badge["x"] - badge["width"] <= 24, (badge, first)
    assert first["x"] >= 0 and last["x"] + last["width"] <= width + 1
    assert active.evaluate(
        """group => group.ownerDocument.documentElement.scrollWidth <=
            group.ownerDocument.documentElement.clientWidth + 1"""
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")


def exercise(page, url, switch, second, shots, _backup):
    mobile = page.context.browser.browser_type.name == "webkit"
    width, height = (390, 844) if mobile else (1366, 768)
    resize_workspace(page, width, height)

    def activate(element):
        if mobile:
            element.tap()
        else:
            element.click()

    first, other = [f"{device.ip}:{device.port}" for device in (switch, second)]
    switch.off_watts = 0
    page.goto(url)
    activate(page.locator('nav a[href="#ports"]'))
    frame = page.frame_locator("#matrixViewer")
    expect_no_quick_power(page)
    page.locator("#queryTargets").fill(f"{first} 1\n{other} 1")
    open_target_editor(page)
    activate(page.locator("#verifyEditor"))
    activate(page.locator("#acceptTargets"))
    expect(page.locator("#targetEditor")).to_be_hidden()
    pending = page.locator(f'#matrixGroups [data-key="{first}|1"]')
    expect(pending).to_be_visible()
    expect_no_quick_power(page)
    activate(pending)
    actions = page.locator("#matrixLegend #quickActions")
    expect(actions).to_be_visible()
    actions.scroll_into_view_if_needed()
    verify_placement(page, frame)
    activate(pending)
    expect_no_quick_power(page)
    assert not switch.commands and not second.commands

    activate(page.locator("#queryPorts"))
    enter_ssh_credentials(page, switch, touch=mobile)
    tile = frame.locator(f'#matrix [data-port-key="{first}/1"]')
    other_tile = frame.locator(f'#matrix [data-port-key="{other}/1"]')
    expect(tile).to_be_visible(timeout=60000)
    expect(page.locator("#queryPorts")).to_be_enabled()
    expect_no_quick_power(page)
    if shots:
        page.screenshot(path=shots / "no-port-selected.png")

    missing = power_filter(frame, "unclassified")
    activate(missing)
    expect(missing).to_have_attribute("aria-pressed", "true")
    expect_no_quick_power(page)
    activate(missing)
    tile.focus()
    expect_no_quick_power(page)
    activate(tile)
    expect(tile).to_have_attribute("aria-pressed", "true")
    verify_placement(page, page)
    if shots:
        page.screenshot(path=shots / "selected-port-power.png")
    if mobile:
        resize_workspace(page, 320, 568)
        verify_placement(page, page)
        if shots:
            page.screenshot(path=shots / "selected-port-power-320.png")
        resize_workspace(page, width, height)

    activate(other_tile)
    expect(page.locator("#selectedCount")).to_have_text("선택 2개")
    activate(page.locator("#listView"))
    verify_placement(page, page)
    activate(missing)
    expect_hidden_selection(page, 2)
    verify_placement(page, page)
    activate(missing)
    for address in (other, first):
        check = frame.locator(f'#tbody tr[data-port-key="{address}/1"] input')
        activate(check)
        expect(check).not_to_be_checked()
    expect_no_quick_power(page)
    row = frame.locator(f'#tbody tr[data-port-key="{first}/1"]')
    row.locator(".row-select").focus()
    expect_no_quick_power(page)
    activate(row.locator(".row-select"))
    expect(page.locator("#controlTarget")).to_contain_text(first)
    verify_placement(page, page)
    activate(row.get_by_role("checkbox"))
    activate(row.get_by_role("checkbox"))
    expect_no_quick_power(page)
    activate(page.locator("#matrixView"))
    other_commands = len(second.commands)
    for operation, before, after, change, delta in (
        ("poe_off", "8.1 W", "0 W", "no interface 1 power-over-ethernet", "\N{MINUS SIGN}8.1 W"),
        ("poe_on", "0 W", "8.1 W", "interface 1 power-over-ethernet", "+8.1 W"),
    ):
        if mobile:
            page.touchscreen.tap(*hit_point(tile))
        else:
            tile.focus()
            tile.press("Space")
        expect(tile).to_have_attribute("aria-pressed", "true")
        with click_quick_poe(page, operation, [(first, "1")], touch=mobile):
            expect_power_result(page, first, before, after, delta=delta)
        expect_no_quick_power(page)
        assert switch.commands.count(change) == 1
        after_write = switch.commands.index(change) + 1
        assert switch.commands[after_write:].count("show power-over-ethernet 1") >= 2
    assert len(second.commands) == other_commands
    page.evaluate("() => api('/api/shutdown', {})")


if __name__ == "__main__":
    mobile = "--webkit" in sys.argv
    run_browser_case(exercise, browser_engine="webkit" if mobile else "chromium", touch=mobile)
    print("PASS selection-only power toolbar, responsive placement, exact SSH writes and readback.")

"""Exercise the selected port's persisted PoE measurements in desktop and touch browsers."""

import re
import sys

from browser_power_matrix import PowerMatrixSwitch
from browser_qa import enter_ssh_credentials, run_browser_case
from browser_unified_controls import resize_workspace
from playwright.sync_api import expect


def selected_plot_position(plot):
    return plot.evaluate("""plot => {
        const point = plot.querySelector('.history-cursor').getBoundingClientRect();
        const area = plot.getBoundingClientRect();
        return {x: point.left + point.width/2 - area.left,
                y: point.top + point.height/2 - area.top};
    }""")


def exercise(page, url, switch, second, shots, _backup):
    mobile = page.context.browser.browser_type.name == "webkit"
    width = 320 if mobile else 1366
    resize_workspace(page, width, 844 if mobile else 768)
    first_address, second_address = (f"127.0.0.1:{item.port}" for item in (switch, second))
    second.draws["1"] = 13
    page.goto(url)
    page.locator("#queryTargets").fill(f"{first_address} 1,5\n{second_address} 1")
    page.locator("#queryPorts").click()
    enter_ssh_credentials(page, switch)
    frame = page.frame_locator("#matrixViewer")
    tiles = frame.locator("#matrix [data-port-key]")
    expect(tiles).to_have_count(3, timeout=90000)
    frame.locator(f'#matrix [data-port-key="{first_address}/1"]').click()

    panel = frame.locator("#powerHistory")
    summary = panel.locator("summary")
    expect(summary).to_have_text("전력 추이")
    assert not panel.evaluate("p => p.open")
    commands = (len(switch.commands), len(second.commands))
    summary.click()
    plot = panel.locator("#powerHistoryPlot")
    latest = panel.locator("#powerHistoryLatest")
    reading = panel.locator("#powerHistoryReading")
    expect(plot).to_have_attribute("data-samples", "1")
    expect(latest).to_contain_text(re.compile(r"\b0\s*W"))
    expect(panel.locator("#powerHistoryTime")).to_contain_text(re.compile(r"\d{1,2}:\d{2}"))
    for hours in ("24", "168", "1"):
        button = panel.locator(f'[data-hours="{hours}"]')
        button.click()
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(plot).to_have_attribute("data-samples", "1")
    assert (len(switch.commands), len(second.commands)) == commands

    # Only the selected endpoint is sampled. The other switch also has a port 1.
    switch.draws["1"] = 4.5
    page.get_by_role("button", name="선택 1포트 재조회", exact=True).click()
    expect(page.locator("#statusText")).to_contain_text("재조회 완료", timeout=90000)
    assert len(second.commands) == commands[1]
    page.locator("#listView").click()
    first_row = frame.locator(f'#tbody tr[data-port-key="{first_address}/1"]')
    first_row.locator(".row-select").click()
    if not panel.evaluate("p => p.open"):
        summary.click()
    commands = (len(switch.commands), len(second.commands))
    expect(plot).to_have_attribute("data-samples", "2")
    expect(latest).to_contain_text("4.5")
    expect(panel.locator("#powerHistoryDelta")).to_contain_text(re.compile(r"\+\s*4\.5"))
    plot.focus()
    plot.press("Home")
    expect(reading).to_contain_text(re.compile(r"\b0\s*W"))
    plot.press("End")
    expect(reading).to_contain_text("4.5")
    expect(first_row.get_by_role("checkbox")).to_be_checked()
    frame.get_by_role("button", name="변화 포트 1개", exact=True).click()
    expect(frame.locator("#tbody tr[data-port-key]")).to_have_count(1)
    expect(first_row.get_by_role("checkbox")).to_be_checked()
    expect(frame.locator("#portComparison")).to_contain_text("0 W")
    expect(frame.locator("#portComparison")).to_contain_text("4.5 W")
    expect(plot).to_have_attribute("data-samples", "2")
    frame.get_by_role("button", name="전체 포트", exact=True).click()

    # No point can be inferred for the port whose PoE CLI returned no measured draw.
    frame.locator(f'#tbody tr[data-port-key="{first_address}/5"] .row-select').click()
    if not panel.evaluate("p => p.open"):
        summary.click()
    expect(panel.locator("#powerHistoryStatus")).to_contain_text("전력 측정값이 없습니다.")
    expect(plot).to_have_attribute("data-samples", "0")
    expect(plot).to_be_hidden()
    frame.locator(f'#tbody tr[data-port-key="{second_address}/1"] .row-select').click()
    if not panel.evaluate("p => p.open"):
        summary.click()
    expect(plot).to_have_attribute("data-samples", "1")
    expect(latest).to_contain_text(re.compile(r"\b13\s*W"))
    assert (len(switch.commands), len(second.commands)) == commands

    # A missing observation cannot be silently bridged or reported as an additional 0 W point.
    for value in (None, 7):
        switch.draws["1"] = value
        page.locator("#queryPorts").click()
        expect(page.locator("#statusText")).to_contain_text("포트 조회 완료", timeout=90000)
        expect(page.locator("#queryPorts")).to_be_enabled()
        expect(tiles).to_have_count(3, timeout=90000)
    page.locator("#listView").click()
    first_row.locator(".row-select").click()
    if not panel.evaluate("p => p.open"):
        summary.click()
    commands = (len(switch.commands), len(second.commands))
    expect(plot).to_have_attribute("data-samples", "3")
    expect(plot).to_have_attribute("data-gaps", "1")
    expect(plot.locator(".history-line")).to_have_count(0)
    expect(latest).to_contain_text(re.compile(r"\b7\s*W"))
    plot.focus()
    plot.press("Home")
    expect(reading).to_contain_text(re.compile(r"\b0\s*W"))
    first_position = selected_plot_position(plot)
    plot.press("ArrowRight")
    expect(reading).to_contain_text("4.5")
    plot.press("End")
    expect(reading).to_contain_text(re.compile(r"\b7\s*W"))
    expect(panel.locator("#powerHistoryDelta")).to_contain_text(re.compile(r"\+\s*2\.5"))
    if mobile:
        # Tap the rendered sample centers regardless of the chosen time scale.
        last_position = selected_plot_position(plot)
        plot.tap(position=first_position)
        expect(plot).to_have_attribute("aria-valuenow", "1")
        expect(reading).to_contain_text(re.compile(r"\b0\s*W"))
        plot.tap(position=last_position)
        expect(plot).to_have_attribute("aria-valuenow", "3")
        expect(reading).to_contain_text(re.compile(r"\b7\s*W"))
    assert frame.locator("html").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert panel.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    if shots:
        panel.screenshot(path=shots / f"power-history-{width}.png")
    assert (len(switch.commands), len(second.commands)) == commands
    page.set_viewport_size({"width": 1366, "height": 768})
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    engine = sys.argv[1] if len(sys.argv) > 1 else "chromium"
    run_browser_case(
        exercise,
        switch_factory=PowerMatrixSwitch,
        second_switch_factory=PowerMatrixSwitch,
        browser_engine=engine,
        touch=engine == "webkit",
    )
    print(f"PASS {engine}: real per-port power history, gaps, range and point selection.")

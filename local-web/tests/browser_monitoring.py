"""Exercise a complete one-minute monitoring interval on two loopback SSH ports."""

import sys

from browser_power_matrix import PowerMatrixSwitch
from browser_qa import enter_ssh_credentials, power_filter, run_browser_case
from browser_unified_controls import hit_point, resize_workspace
from playwright.sync_api import expect


def exercise(page, url, switch, _second, shots, _backup):
    address = f"127.0.0.1:{switch.port}"
    page.goto(url)
    page.locator('nav a[href="#ports"]').click()
    page.locator("#queryTargets").fill(f"{address} 1,3")
    toggle = page.locator("#monitorToggle")
    interval = page.locator("#monitorInterval")
    monitor_status = page.locator("#monitorStatus")
    expect(interval).to_have_value("5")
    for minutes, valid in (("0", False), ("61", False), ("1.5", False), ("60", True), ("1", True)):
        interval.fill(minutes)
        assert interval.evaluate("input => input.checkValidity()") is valid

    toggle.click()
    enter_ssh_credentials(page, switch)
    frame = page.frame_locator("#matrixViewer")
    tiles = frame.locator("#matrix [data-port-key]")
    expect(tiles).to_have_count(2, timeout=90000)
    expect(page.locator("#matrixView")).to_have_attribute("aria-pressed", "true")
    expect(toggle).to_have_attribute("aria-pressed", "true")
    expect(monitor_status).to_be_visible()
    initial = page.evaluate("() => api('/api/monitor')")
    assert initial["active"] and initial["intervalMinutes"] == 1 and initial["portCount"] == 2

    # Only port 3 is visible and checked. The scheduler must still collect port 1.
    key = f"{address}/3"
    frame.locator(f'#matrix [data-port-key="{key}"]').click()
    page.locator("#listView").click()
    frame.locator("#search").fill("matrix-ap-3")
    normal = power_filter(frame, "normal")
    normal.click()
    row = frame.locator(f'#tbody tr[data-port-key="{key}"]')
    expect(frame.locator("#tbody tr[data-port-key]")).to_have_count(1)
    expect(row.locator("td").nth(2)).to_have_text("8 W")
    row.locator("input").check()
    expect(row).to_have_class("selected")
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    commands = [
        f"show {command} {port}"
        for port in ("1", "3")
        for command in ("power-over-ethernet", "lldp info remote-device")
    ]
    reads = {command: switch.commands.count(command) for command in commands}
    assert all(reads.values())
    switch.draws.update({"1": 7, "3": 12})
    switch.ap_names["1"] = "new-ap-1"

    for width, height in ((1366, 768), (390, 844), (320, 568)):
        resize_workspace(page, width, height)
        expect(toggle).to_be_visible()
        toggle.scroll_into_view_if_needed()
        hit_point(toggle)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if shots:
            page.screenshot(path=shots / f"monitoring-{width}.png")

    # Wait for the real scheduler and switch, without a manual refresh or clock override.
    expect(row.locator("td").nth(2)).to_have_text("12 W", timeout=150000)
    assert all(switch.commands.count(command) > reads[command] for command in commands)
    port_reads = {
        command
        for command in switch.commands
        if command.startswith(("show power-over-ethernet ", "show lldp info remote-device "))
    }
    assert port_reads == set(commands)
    expect(frame.locator("#search")).to_have_value("matrix-ap-3")
    expect(normal).to_have_attribute("aria-pressed", "true")
    expect(row).to_have_class("selected")
    expect(row.locator("input")).to_be_checked()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    expect(page.locator("#listView")).to_have_attribute("aria-pressed", "true")
    recorded = page.evaluate("() => api('/api/monitor')")
    assert recorded["monitorId"] == initial["monitorId"] and recorded["lastSuccess"]

    toggle.click()
    page.wait_for_function("async () => !(await api('/api/monitor')).active")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    resize_workspace(page, 1366, 768)
    frame.locator("#search").fill("")
    normal.click()
    hidden = frame.locator(f'#tbody tr[data-port-key="{address}/1"]')
    expect(hidden.locator("td").nth(2)).to_have_text("7 W")
    expect(hidden.locator("td").nth(3)).to_have_text("new-ap-1")
    # Both real scheduler reads must be present, including the port hidden during that interval.
    for port, first, second in (("1", 0, 7), ("3", 8, 12)):
        history = page.evaluate(
            "url => api(url)",
            f"/api/power-history?ip=127.0.0.1&ssh_port={switch.port}&interface={port}&hours=1",
        )
        prior, latest = history["points"][:2]
        assert (prior["watts"], latest["watts"], latest["previousWatts"]) == (first, second, first)
        assert not prior["connected"] and latest["connected"]
        assert prior["at"] < latest["at"]
    hidden.locator(".row-select").click()
    history_panel = frame.locator("#powerHistory")
    history_panel.locator("summary").click()
    expect(history_panel.locator(".history-line")).to_have_count(1)
    expect(history_panel.locator("#powerHistoryLatest")).to_have_text("7 W")
    if shots:
        history_panel.screenshot(path=shots / "monitoring-power-history.png")
    assert "configure terminal" not in switch.commands
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    engine = sys.argv[1] if len(sys.argv) > 1 else "chromium"
    run_browser_case(exercise, switch_factory=PowerMatrixSwitch, browser_engine=engine)
    print(f"PASS {engine}: one-minute full-scope monitoring and mobile view/selection retention.")

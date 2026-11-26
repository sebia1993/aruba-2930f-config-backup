"""Verify FIFO reservations during a real one-minute monitor cycle on loopback SSH."""

import re
from contextlib import contextmanager
from threading import Event
from unittest.mock import patch

from browser_cable import CableSwitch
from browser_qa import enter_ssh_credentials, quick_power_group, run_browser_case
from playwright.sync_api import expect


@contextmanager
def hold_next_read(switch):
    """Leave one read in flight while the operator works in the browser."""
    reached, resume = Event(), Event()
    respond = switch._respond

    def hold(channel, command):
        if command == "show power-over-ethernet 1" and not reached.is_set():
            reached.set()
            assert resume.wait(30), "Browser did not release the simulated switch read"
        return respond(channel, command)

    with patch.object(switch, "_respond", hold):
        try:
            yield reached, resume
        finally:
            resume.set()


def select_only(tiles, chosen):
    for key, tile in tiles.items():
        if (tile.get_attribute("aria-pressed") == "true") != (key == chosen):
            tile.click()
        expect(tile).to_have_attribute("aria-pressed", "true" if key == chosen else "false")


def submit(page, button, action):
    def matching(response):
        return (
            response.request.method == "POST"
            and response.url.endswith("/api/jobs")
            and response.request.post_data_json["action"] == action
        )

    with page.expect_response(matching) as response:
        button.click()
    return response.value


def exercise(page, url, power_switch, cable_switch, _shots, _backup):
    power_switch.off_watts = 0
    power = f"{power_switch.ip}:{power_switch.port}"
    cable = f"{cable_switch.ip}:{cable_switch.port}"
    page.goto(url)
    page.locator('nav a[href="#ping"]').click()
    expect(page.locator("#pingHeading")).to_be_visible()
    page.locator("#pingTargets").fill("192.0.2.11")
    page.locator("#pingDestinations").fill("198.51.100.10")
    page.locator('[data-ping-preset="quick"]').click()
    expect(page.locator("#pingRepetitions")).to_have_value("3")
    expect(page.locator("#pingTimeout")).to_have_value("1")
    page.locator("#pingPreview").click()
    expect(page.locator("#pingCommands")).to_have_value(
        "ping 198.51.100.10 repetitions 3 timeout 1 source 192.0.2.11"
    )
    expect(page.locator("#pingPreviewSummary")).to_contain_text("총 3회")
    expect(page.locator("#pingCopy")).to_be_enabled()
    page.locator("#pingSourceMode").select_option("vlan")
    expect(page.locator("#pingVlan")).to_be_visible()
    expect(page.locator("#pingCommands")).to_have_value("")
    expect(page.locator("#pingCopy")).to_be_disabled()
    page.locator("#pingVlan").fill("10")
    page.locator("#pingPreview").click()
    expect(page.locator("#pingCommands")).to_have_value(
        "ping 198.51.100.10 repetitions 3 timeout 1 source 10"
    )
    expect(page.locator("#pingPreviewStatus")).to_contain_text("미실행")
    page.locator('nav a[href="#ports"]').click()
    page.locator("#queryTargets").fill(f"{power} 1\n{cable} 1")
    expect(page.locator("#monitorInterval")).to_have_value("1")
    page.locator("#monitorToggle").click()
    enter_ssh_credentials(page, power_switch)
    frame = page.frame_locator("#matrixViewer")
    tiles = {
        address: frame.locator(f'#matrix [data-port-key="{address}/1"]')
        for address in (power, cable)
    }
    expect(tiles[power]).to_be_visible(timeout=90000)
    expect(page.locator("#monitorStatus")).to_contain_text("다음", timeout=90000)
    select_only(tiles, power)

    queue = page.locator("#manualQueue")

    def job(ident):
        return queue.locator(f'[data-job-id="{ident}"]')

    quick_action = "control.quick-execute"
    write_off = "no interface 1 power-over-ethernet"
    with hold_next_read(power_switch) as (reached, resume):
        assert reached.wait(100), "The next one-minute read never reached the SSH fixture"
        monitor = page.evaluate("() => api('/api/monitor')")
        assert monitor["active"] and monitor["jobId"] and monitor["intervalMinutes"] == 1

        off = quick_power_group(page).locator("#quickOff")
        queued = submit(page, off, quick_action)
        assert queued.ok, queued.text()
        first = queued.json()["id"]
        expect(job(first)).to_have_attribute("data-state", "queued")
        # Reapplying the same intent must keep a single reservation. The opposite
        # intent must leave the original pending until the operator cancels it.
        select_only(tiles, power)
        recovered = []

        def lose_ack(route):
            response = route.fetch()
            assert response.status == 202, response.text()
            recovered.append(response.json()["id"])
            route.abort()

        page.route("**/api/jobs", lose_ack, times=1)
        off.click()
        expect(queue.locator("#manualQueueNotice")).to_contain_text("접수 확인 완료", timeout=30000)
        assert recovered == [first]
        select_only(tiles, power)
        conflict = submit(page, quick_power_group(page).locator("#quickOn"), quick_action)
        assert conflict.status == 409 and first in conflict.json()["conflicts"], conflict.text()
        expect(job(first)).to_have_attribute("data-state", "queued")
        expect(queue.locator('[data-state="queued"]')).to_have_count(1)
        job(first).get_by_role("button", name="예약 취소").click()
        expect(job(first)).to_have_attribute("data-state", "cancelled")
        assert write_off not in power_switch.commands

        select_only(tiles, cable)
        page.locator('nav a[href="#cable"]').click()
        page.locator("#cableTest").click()
        review = page.locator("#review")
        expect(review).to_contain_text(cable)
        expect(review).to_contain_text("1개 포트")
        expect(review).to_contain_text("선택하지 않은 포트")
        expect(review).to_contain_text("링크")
        expect(review).not_to_contain_text(power)
        cable_response = submit(page, page.locator("#cableConfirm"), "cable.test")
        assert cable_response.ok, cable_response.text()
        cable_id = cable_response.json()["id"]
        expect(job(cable_id)).to_have_attribute("data-state", "queued")
        expect(review).to_be_hidden()
        assert not cable_switch.tested

        page.locator('nav a[href="#ports"]').click()
        select_only(tiles, power)
        second = submit(page, off, quick_action)
        assert second.ok, second.text()
        off_id = second.json()["id"]
        assert off_id not in (first, cable_id)
        expect(job(off_id)).to_have_attribute("data-state", "queued")
        page.reload()
        expect(job(cable_id)).to_have_attribute("data-state", "queued")
        expect(job(off_id)).to_have_attribute("data-state", "queued")
        assert write_off not in power_switch.commands and not cable_switch.tested

        cable_switch.release_result.clear()
        try:
            resume.set()
            assert cable_switch.confirmed.wait(45), "Approved cable job did not run first"
            expect(job(cable_id)).to_have_attribute("data-state", "running")
            expect(job(off_id)).to_have_attribute("data-state", "queued")
            assert write_off not in power_switch.commands
            expect(page.locator("#review")).to_be_hidden()
        finally:
            cable_switch.release_result.set()

    expect(job(cable_id)).to_have_attribute("data-state", "done", timeout=90000)
    expect(job(off_id)).to_have_attribute("data-state", "done", timeout=90000)
    expect(job(first)).to_have_attribute("data-state", "cancelled")
    assert cable_switch.tested == ["1"] and cable_switch.confirmations == [b"y"]
    assert cable_switch.commands.count("clear cable-diagnostics") == 1
    assert power_switch.commands.count(write_off) == 1
    assert "interface 1 power-over-ethernet" not in power_switch.commands
    writes = power_switch.commands.index(write_off)
    assert power_switch.commands[writes + 1 :].count("show power-over-ethernet 1") >= 2
    if not page.locator("#manualQueueDetails").evaluate("details => details.open"):
        page.locator("#manualQueueDetails summary").click()
    result = page.locator("#review")
    job(cable_id).get_by_role("button", name="결과 보기").click()
    expect(result).to_contain_text(re.compile(r"17\s*m"))
    result.get_by_role("button", name="검토 닫기").click()
    job(off_id).get_by_role("button", name="결과 보기").click()
    expect(result).to_contain_text("검증 완료")
    expect(result).to_contain_text("0 W")
    result.get_by_role("button", name="검토 닫기").click()
    page.locator("#listView").click()
    row = frame.locator(f'#tbody tr[data-port-key="{power}/1"]')
    expect(row.locator("td").nth(2)).to_have_text("0 W", timeout=30000)
    row.get_by_role("checkbox").check()
    expect(quick_power_group(page).locator("#quickOn")).to_be_enabled()
    assert page.evaluate("() => api('/api/monitor')")["active"]
    page.evaluate("() => api('/api/shutdown', {})")


if __name__ == "__main__":
    run_browser_case(exercise, second_switch_factory=CableSwitch)
    print("PASS monitor, FIFO, cancellation, conflicts, reload, cable and PoE readback.")

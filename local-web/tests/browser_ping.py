"""Exercise the real UI, HTTP handlers and database with a deterministic device boundary."""

import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_qa import launch_test_browser
from localweb.jobs import Jobs
from localweb.ping_ssh import PingPairResult
from localweb.ping_tools import PingObservation
from localweb.sharing import Sharing
from server import Handler, LocalServer

from aruba2930f_backup.models import Credentials, DeviceIdentity


def observe(plan, target, destination, credentials, cancel, *, before_send=None):
    if before_send:
        before_send()
    count = plan.options.repetitions
    return PingPairResult(
        DeviceIdentity("fixture", "2930F", "JL255A", "WC.16.11.0010"),
        PingObservation("normal", count, count, 0, 1, 1, 1),
        plan.requested_source(target),
        target.ip,
        True,
    )


@contextmanager
def fake_device(_plan, target, credentials, _cancel):
    def run(plan, destination, *, cancel, before_send=None):
        return observe(plan, target, destination, credentials, cancel, before_send=before_send)

    yield SimpleNamespace(run_pair=run)


def exercise(page, url):
    errors = []
    starts = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "request",
        lambda request: (
            starts.append(request.url)
            if request.method == "POST"
            and request.url.endswith(("/api/ping/runs", "/api/ping/long/runs"))
            else None
        ),
    )
    page.goto(url)
    page.evaluate("location.hash = '#ping'")
    expect(page.locator('[data-ping-pane="measure"]')).to_have_count(1, timeout=2000)
    expect(page.locator('[data-ping-pane="general"], [data-ping-pane="long"]')).to_have_count(0)
    expect(page.locator("#pingInterval")).to_have_value("0")
    expect(page.locator("#pingRepetitions")).to_have_value("5")
    # A crash between saving a quick request ID and admitting it on the server
    # must allow explicit recovery without posting a replacement in either mode.
    page.evaluate("sessionStorage.setItem('ping.run.id.v1', '0'.repeat(32))")
    page.reload()
    page.locator("#pingInputMode").select_option("list")
    page.locator("#pingTargets").fill("192.0.2.17")
    page.locator("#pingDestinations").fill("198.51.100.20")
    page.locator("#pingInterval").select_option("1")
    expect(page.locator("#pingStart")).to_be_disabled()
    expect(page.locator("#pingNew")).to_be_enabled(timeout=6000)
    page.locator("#pingNew").click()
    expect(page.locator("#pingRequestId")).to_have_text("—")
    expect(page.locator("#pingStart")).to_be_enabled()
    assert not starts
    page.locator("#pingOptions > summary").click()
    page.locator("#pingLongHours").fill("2")
    timing = page.locator("#pingLongTiming")
    expect(timing).to_be_visible()
    expect(timing).to_contain_text("KST · 매회 응답 타임아웃 시 예상 소요 00:")
    expect(timing).to_contain_text("실행 한도 2시간")
    page.locator("#pingOptions > summary").click()
    page.locator("#pingInterval").select_option("0")
    page.locator("#pingInputMode").select_option("list")
    expect(page.locator("#pingTargets")).to_be_visible()
    page.locator("#pingTargets").fill("192.0.2.17")
    page.locator("#pingDestinations").fill("198.51.100.20\tNMS")
    page.locator("#pingProfiles summary").click()
    page.locator("#pingProfileName").fill("브라우저 검증")
    page.locator("#pingProfileSave").click()
    expect(page.locator("#pingProfileStatus")).to_contain_text("저장했습니다")
    page.locator("#pingTargets").fill("")
    page.locator("#pingProfileLoad").click()
    expect(page.locator("#pingTargets")).to_have_value("192.0.2.17:22")
    page.locator("#pingProfiles summary").click()
    page.locator("#pingPreview").click()
    page.locator("#pingStart").click()
    expect(page.locator("#pingRunStatus")).to_contain_text("작업 종료", timeout=20000)
    with page.expect_download() as download:
        page.locator("#pingExportFiltered").click()
    output = load_workbook(BytesIO(Path(download.value.path()).read_bytes()), read_only=True)
    try:
        assert output.sheetnames == ["요약", "상세 결과"]
        assert list(output["상세 결과"].values)[1][4] == "198.51.100.20"
    finally:
        output.close()
    page.locator("#pingInterval").select_option("1")
    page.locator("#pingRepetitions").fill("3")
    expect(page.locator("#pingRepetitions")).to_be_focused()
    page.locator("#pingStart").click()
    page.locator("#pingLongPause").click()
    expect(page.locator("#pingLongResume")).to_be_enabled(timeout=10000)
    # A paused periodic run still owns the measurement even with a valid quick input.
    if page.locator("#pingInterval").is_enabled():
        page.locator("#pingInterval").select_option("0")
        expect(page.locator("#pingStart")).to_be_disabled()
        expect(page.locator("#pingLongResume")).to_be_visible()
        page.locator("#pingInterval").select_option("1")
    status_url = "**/api/ping/long/runs/*"

    def block_status_read(route):
        if route.request.method == "GET":
            route.abort("internetdisconnected")
        else:
            route.continue_()

    page.route(status_url, block_status_read)
    page.locator("#pingLongRefresh").click()
    expect(page.locator("#pingLongTime")).to_contain_text("갱신 지연")
    expect(page.locator("#pingLongStatus")).to_contain_text("마지막 확인 일시정지")
    page.unroute(status_url, block_status_read)
    page.reload()
    expect(page.locator("#pingLongResume")).to_be_enabled(timeout=10000)
    expect(page.locator("#pingInterval")).to_have_value("1")
    expect(page.locator("#pingRepetitions")).to_have_value("3")
    expect(page.locator("#pingStart")).to_be_disabled()
    page.route(status_url, block_status_read)
    page.locator("#pingLongResume").click()
    expect(page.locator("#pingLongStatus")).to_contain_text("측정 중")
    page.locator("#pingLongRefresh").click()
    expect(page.locator("#pingLongTime")).to_contain_text("갱신 지연")
    stale_time = page.locator("#pingLongTime").inner_text()
    stale_value = page.locator("#pingLongProgress").get_attribute("value")
    # The real backend continues its accepted run. The disconnected tab must
    # keep the last server evidence and freeze the countdown across timer ticks.
    page.wait_for_timeout(2200)
    assert page.locator("#pingLongProgress").get_attribute("value") == stale_value
    expect(page.locator("#pingLongTime")).to_have_text(stale_time)
    expect(page.locator("#pingLongStatus")).not_to_contain_text("목표 완료")
    page.unroute(status_url, block_status_read)
    page.locator("#pingLongRefresh").click()
    expect(page.locator("#pingLongStatus")).to_contain_text("목표 완료", timeout=24000)
    with page.expect_download() as download:
        page.locator("#pingLongExcel").click()
    book = load_workbook(BytesIO(Path(download.value.path()).read_bytes()), read_only=True)
    try:
        samples = list(book["개별 Ping"].values)
        attempt = samples[0].index("attempt")
        sent = samples[0].index("sent")
        assert [(row[attempt], row[sent]) for row in samples[1:] if row[sent]] == [
            (1, 1),
            (2, 1),
            (3, 1),
        ]
    finally:
        book.close()
    page.locator('[data-ping-pane="analysis"]').click()
    page.locator("#paLoad").click()
    expect(page.locator("#paRun option")).to_have_count(1, timeout=6000)
    page.locator("#paReport").click()
    expect(page.locator("#paMetrics")).to_contain_text("P95")
    assert page.locator("#paSamples table tbody tr").count() >= 3
    page.locator('[data-pa-tab="heat"]').click()
    page.locator('[data-ping-pane="path"]').click()
    page.locator("#pingTraceHistory").click()
    expect(page.locator("#pingTraceStatus")).to_have_text("최근 0건")
    for width in (768, 390):
        page.set_viewport_size({"width": width, "height": 840})
        for pane in ("measure", "analysis", "path"):
            page.locator(f'[data-ping-pane="{pane}"]').click()
            overflow = page.evaluate("""() => document.documentElement.scrollWidth <= innerWidth + 2 ? [] :
              [...document.querySelectorAll('body *')].filter(e => e.clientWidth && e.getBoundingClientRect().right > innerWidth + 2)
                .slice(0,10).map(e => [e.id || e.className || e.tagName, Math.ceil(e.getBoundingClientRect().right)])""")
            assert not overflow, (width, pane, overflow)
    if os.environ.get("ARUBA_WEB_SCREENSHOTS"):
        directory = Path(os.environ["ARUBA_WEB_SCREENSHOTS"])
        directory.mkdir(parents=True, exist_ok=True)
        page.locator('[data-ping-pane="measure"]').click()
        page.screenshot(path=directory / "ping-mobile.png")
    assert not errors, errors
    assert len(starts) == 2 and starts[0].endswith("/api/ping/runs")
    assert starts[1].endswith("/api/ping/long/runs")


def run_browser_test(flow, device_session=fake_device, *, account_set=True):
    with (
        tempfile.TemporaryDirectory() as tmp,
        LocalServer(("127.0.0.1", 0), Handler) as app,
        sync_playwright() as pw,
    ):
        app.jobs = Jobs(Path(tmp))
        if account_set:
            app.jobs.operations.credentials = Credentials("fixture", "fixture")
        app.launch, app.cookie_name, app.cookie, app.token, app.instance = (
            "launch",
            "TestLocal",
            "cookie",
            "token",
            "test",
        )
        app.sharing = Sharing(app, Path(tmp), port=0)
        worker = threading.Thread(
            target=app.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        with patch.object(app.jobs.operations, "_ping_device_session", side_effect=device_session):
            worker.start()
            try:
                browser = launch_test_browser(pw.chromium)
                try:
                    flow(
                        browser.new_page(viewport={"width": 1440, "height": 960}),
                        f"http://127.0.0.1:{app.server_port}/start/launch",
                    )
                finally:
                    browser.close()
            finally:
                app.shutdown()
                worker.join(3)
                app.sharing.close()


if __name__ == "__main__":
    run_browser_test(exercise)
    print(
        "PASS Ping profiles, XLSX, bounded run, pause/reload/resume, history and responsive views."
    )

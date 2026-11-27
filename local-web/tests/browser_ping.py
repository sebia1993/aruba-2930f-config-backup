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
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url)
    page.evaluate("location.hash = '#ping'")
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
    page.locator('[data-ping-pane="long"]').click()
    page.locator("#pingLongCount").fill("3")
    page.locator("#pingLongInterval").select_option("1")
    page.locator("#pingLongPreview").click()
    expect(page.locator("#pingLongEstimate")).to_contain_text("총 3회")
    page.locator("#pingLongStart").click()
    page.locator("#pingLongPause").click()
    expect(page.locator("#pingLongResume")).to_be_enabled(timeout=10000)
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
    page.locator('[data-ping-pane="long"]').click()
    expect(page.locator("#pingLongResume")).to_be_enabled(timeout=10000)
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
        for pane in ("general", "long", "analysis", "path"):
            page.locator(f'[data-ping-pane="{pane}"]').click()
            overflow = page.evaluate("""() => document.documentElement.scrollWidth <= innerWidth + 2 ? [] :
              [...document.querySelectorAll('body *')].filter(e => e.clientWidth && e.getBoundingClientRect().right > innerWidth + 2)
                .slice(0,10).map(e => [e.id || e.className || e.tagName, Math.ceil(e.getBoundingClientRect().right)])""")
            assert not overflow, (width, pane, overflow)
    if os.environ.get("ARUBA_WEB_SCREENSHOTS"):
        directory = Path(os.environ["ARUBA_WEB_SCREENSHOTS"])
        directory.mkdir(parents=True, exist_ok=True)
        page.locator('[data-ping-pane="long"]').click()
        page.screenshot(path=directory / "ping-mobile.png")
    assert not errors, errors


def main():
    with (
        tempfile.TemporaryDirectory() as tmp,
        LocalServer(("127.0.0.1", 0), Handler) as app,
        sync_playwright() as pw,
    ):
        app.jobs = Jobs(Path(tmp))
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
        with (
            patch.object(app.jobs.operations, "_ping_device_session", side_effect=fake_device),
            patch.object(app.jobs.operations, "_ping_pair", side_effect=observe),
        ):
            worker.start()
            try:
                browser = launch_test_browser(pw.chromium)
                try:
                    exercise(
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
    main()
    print(
        "PASS Ping profiles, XLSX, bounded run, pause/reload/resume, history and responsive views."
    )

"""Use installed Chrome and a loopback SSH fixture to exercise the local browser UI."""

import json
import os
import re
import site
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Event
from unittest.mock import patch
from urllib.parse import urljoin

from playwright.sync_api import expect, sync_playwright
from verify_http_ssh import MonitorFixture


def open_port_controls(page):
    if not page.locator("#controlPanel").evaluate("panel => panel.open"):
        page.locator("#controlHeading").click()


def edit_query_targets(page):
    targets = page.locator("#queryTargets")
    if targets.is_hidden():
        page.locator("#editQueryTargets").click()
    expect(targets).to_be_editable()
    return targets


def enter_ssh_credentials(page, switch, *, touch=False):
    dialog = page.get_by_role("dialog", name="SSH 접속 계정", exact=True)
    expect(dialog).to_be_visible()
    dialog.locator("#queryUsername").fill(switch.username)
    dialog.locator("#queryPassword").fill(switch.password)
    submit = dialog.get_by_role("button", name="계정 적용 후 계속")
    if touch:
        submit.tap()
    else:
        submit.click()
    expect(dialog).to_be_hidden()
    expect(page.locator("#queryPassword")).to_have_value("")


def expect_power_result(page, endpoint, before, after, *, port="1", delta=None):
    table = page.locator("#matrixWorkspace #controlResult table")
    row = table.locator("tbody tr").filter(has=page.get_by_text(endpoint, exact=True))
    row = row.filter(has=page.get_by_role("cell", name=port, exact=True))
    expect(row).to_have_count(1, timeout=90000)
    headers = table.locator("thead th").all_text_contents()
    for label, value in (("변경 전 실측 전력", before), ("자동 재조회 전력", after)):
        cell = row.get_by_role("cell").nth(headers.index(label))
        expect(cell.locator("strong")).to_have_text(value)
        if value == "미측정":
            expect(cell).not_to_contain_text("0 W")
    change = row.locator(".power-delta")
    if delta is None:
        expect(change).to_have_count(0)
    else:
        expect(change).to_have_text("변화 " + delta)


@contextmanager
def click_quick_poe(page, operation, targets, *, touch=False):
    """Verify one click or tap submits exactly one job for the selected SSH endpoints."""
    jobs = []

    def record(request):
        if request.method == "POST" and request.url.endswith("/api/jobs"):
            jobs.append(request.post_data_json)

    review_monitor = page.locator("#review").evaluate_handle("""dialog => {
        let opened = false;
        const observe = event => { if (event.newState === "open") opened = true; };
        dialog.addEventListener("beforetoggle", observe);
        return () => {
            dialog.removeEventListener("beforetoggle", observe);
            return opened;
        };
    }""")
    page.on("request", record)
    try:
        name = "PoE ON 즉시 실행" if operation == "poe_on" else "PoE OFF 즉시 실행"
        with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/jobs")):
            card = page.locator("#matrixWorkspace")
            button = card.get_by_role("button", name=name, exact=True)
            if touch:
                button.tap()
            else:
                button.click()
        assert len(jobs) == 1 and jobs[0]["action"] == "control.quick-execute", jobs
        body = jobs[0]["body"]
        assert re.fullmatch(r"[0-9a-f]{32}", body["requestId"])
        assert body["operation"] == operation
        requested = [(f"{t['ip']}:{t['sshPort']}", t["port"]) for t in body["targets"]]
        assert sorted(requested) == sorted(targets), requested
        yield
        expect(page.locator("#review")).to_be_hidden()
        assert len(jobs) == 1, jobs
    finally:
        page.remove_listener("request", record)
        opened = review_monitor.evaluate("stop => stop()")
        review_monitor.dispose()
        assert not opened, "Quick PoE opened an approval dialog"


def assert_theme_action(page, mode):
    label, icon = ("라이트", "sun") if mode == "dark" else ("다크", "moon")
    expect(page.locator("html")).to_have_attribute("data-theme", mode)
    actions = page.get_by_role("button", name=re.compile("라이트|다크"))
    expect(actions).to_have_count(1)
    expect(actions).to_have_accessible_name(label + " 모드로 전환")
    expect(actions).to_have_text(label)
    expect(actions.locator("svg")).to_be_visible()
    expect(actions.locator("use")).to_have_attribute("href", "#i-" + icon)


def verify_standalone_themes(page):
    viewer = page.context.browser.new_page(storage_state=page.context.storage_state())
    try:
        viewer.goto(urljoin(page.url, "/matrix"))
        assert_theme_action(viewer, "dark")
        toggle = viewer.locator("#themeToggle")
        toggle.press("Space")
        assert_theme_action(viewer, "light")
        viewer.reload()
        assert_theme_action(viewer, "light")
        toggle.press("Enter")
        assert_theme_action(viewer, "dark")
    finally:
        viewer.close()


def verify_retired_port_files(page, data_folder):
    # Simulate an upgrade with a real pre-existing handle, without touching the user's files.
    old = data_folder / "outputs" / "prior-release" / "port-query.csv"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"ip,interface\r\n")
    handle = "0" * 31 + "1"
    with closing(sqlite3.connect(data_folder / "web-history.sqlite")) as database, database:
        database.execute(
            "INSERT INTO downloads (id,path) VALUES (?,?)",
            (handle, str(old.relative_to(data_folder))),
        )
        files = json.dumps([{"name": old.name, "id": handle}])
        for job_id, action in (("old-export", "ports.export"), ("old-query", "ports.query")):
            database.execute(
                "INSERT INTO jobs(id,action,state,summary,files) VALUES(?,?,?,?,?)",
                (job_id, action, "done", "이전 조회", files),
            )
    for path in ("/csv", "/static/csv.html", "/download/" + handle):
        assert page.request.get(urljoin(page.url, path)).status == 404
    token = page.request.get(urljoin(page.url, "/api/bootstrap")).json()["token"]
    response = page.request.post(
        urljoin(page.url, "/api/jobs"),
        headers={"X-Local-Token": token},
        data={"action": "ports.export", "body": {}},
    )
    assert response.status == 400
    history = page.request.get(urljoin(page.url, "/api/history")).json()["items"]
    assert all(item["action"] != "ports.export" for item in history)
    assert next(item["files"] for item in history if item["id"] == "old-query") == []
    assert old.is_file()


def exercise(page, url, switch, second_switch, shots, backup_directory):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url)
    verify_retired_port_files(page, backup_directory.parent)
    expect(page.locator("#version")).to_contain_text("v1.")
    expect(page.locator('nav a[aria-current="page"]')).to_have_attribute("href", "#ports")
    expect(page.locator('[data-page="ports"]')).to_be_visible()
    links = page.get_by_role("navigation", name="주 메뉴").get_by_role("link")
    assert links.evaluate_all("links => links.map(link => link.hash)") == [
        "#ports",
        "#cable",
        "#backup",
    ]
    expect(page.locator('#logViewer, [data-page="logs"], #logForm')).to_have_count(0)
    for path in ("/logs", "/static/logs.html"):
        assert page.request.get(urljoin(page.url, path)).status == 404
    if shots:
        page.screenshot(path=shots / "01-local-port-dashboard.png")

    def nav(key):
        page.locator(f'nav a[href="#{key}"]').click()
        expect(page.locator(f'[data-page="{key}"]')).to_be_visible()

    assert_theme_action(page, "dark")
    toggle = page.locator("#themeToggle")
    toggle.press("Space")
    assert_theme_action(page, "light")
    page.set_viewport_size({"width": 390, "height": 844})
    assert_theme_action(page, "light")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if shots:
        page.screenshot(path=shots / "02-mobile-port-dashboard.png")

    # The embedded matrix follows the host theme across a reload.
    nav("ports")
    matrix = page.frame_locator("#matrixViewer")
    expect(matrix.locator("html")).to_have_attribute("data-theme", "light")
    page.reload()
    assert_theme_action(page, "light")
    expect(matrix.locator("#themeToggle")).to_be_hidden()
    expect(matrix.locator("html")).to_have_attribute("data-theme", "light")
    toggle.press("Enter")
    assert_theme_action(page, "dark")
    expect(matrix.locator("html")).to_have_attribute("data-theme", "dark")
    page.set_viewport_size({"width": 1440, "height": 960})
    verify_standalone_themes(page)

    expect(page.locator('a[href="#connect"]')).to_have_count(0)
    expect(page.locator("#sessionChip")).to_have_count(0)
    expect(page.locator('[data-page="connect"]')).to_be_hidden()
    # Advanced key maintenance remains available via an explicitly entered address.
    page.evaluate("location.hash = 'connect'")
    expect(page.locator('[data-page="connect"]')).to_be_visible()
    address = f"127.0.0.1:{switch.port}"
    second_address = f"127.0.0.1:{second_switch.port}"
    page.locator("#trustTargets").fill(address + "\n" + second_address)
    page.locator("#inspectForm button").click()
    expect(page.locator("#trustResult")).to_contain_text("SHA256:")
    for endpoint in (address, second_address):
        row = page.locator("#trustResult .entry").filter(has_text=endpoint)
        row.get_by_role("button", name="지문 검토").click()
        expect(page.locator("#reviewBody")).to_contain_text("SHA256:")
        page.get_by_role("button", name="이 지문 승인").click()
        expect(row).to_contain_text("등록된 키")
    nav("backup")
    expect(page.locator("#concurrency")).to_have_value("10")
    expect(page.locator("#attempts")).to_have_value("4")
    page.locator("#backupDirectory").fill(str(backup_directory))
    page.locator("#backupTargets").fill(address)
    # A second tab opened during the backup must recover the same result and files.
    reading, resume = Event(), Event()
    respond = switch._respond

    def hold_config_read(channel, command):
        if command == "show running-config":
            reading.set()
            assert resume.wait(45), "Backup result check did not resume the SSH read"
        return respond(channel, command)

    with patch.object(switch, "_respond", hold_config_read):
        try:
            page.locator("#backupForm .primary").click()
            enter_ssh_credentials(page, switch)
            assert reading.wait(30), "Backup did not reach the loopback switch"
            recovery_context = page.context.browser.new_context(
                storage_state=page.context.storage_state()
            )
            reopened = recovery_context.new_page()
            with reopened.expect_response("**/api/bootstrap") as bootstrap:
                reopened.goto(page.url)
            assert bootstrap.value.json()["busyJob"]
        finally:
            resume.set()

    expect(page.locator('[data-page="backup"]')).to_be_visible()
    expect(page.locator("#backupResult")).to_contain_text("성공", timeout=90000)
    expect(reopened.locator("#backupResult")).to_contain_text("성공", timeout=90000)
    resumed_report = reopened.locator('#backupFiles a[download$=".xlsx"]')
    with reopened.expect_download() as recovered_download:
        resumed_report.click()
    assert (
        recovered_download.value.path().read_bytes()
        == next(backup_directory.rglob("result.xlsx")).read_bytes()
    )
    recovery_context.close()
    expect(page.locator("#backupResult")).to_contain_text(str(backup_directory))
    expect(page.locator("#backupResult")).to_contain_text("실제 저장 결과 폴더")
    assert list(backup_directory.rglob("result.xlsx"))
    # Exercise the real UI request shape without opening an Explorer window during QA.
    opened = []

    def intercept_folder(route):
        assert route.request.method == "POST"
        assert route.request.headers.get("x-local-token")
        opened.append(route.request.post_data_json["fileId"])
        route.fulfill(json={"opened": True})

    page.route("**/api/open-folder", intercept_folder)
    page.locator("#backupResult").get_by_role("button", name="결과 폴더 열기").click()
    page.wait_for_function("() => document.getElementById('backupFiles').children.length > 0")
    report = page.locator('#backupFiles a[download$=".xlsx"]')
    with page.expect_download() as report_event:
        report.click()
    assert report_event.value.path().read_bytes()[:2] == b"PK"
    nav("ports")
    expect(page.get_by_role("link", name="포트·PoE 관리", exact=True)).to_have_count(1)
    expect(page.locator('nav a[href="#csv"]')).to_have_count(0)
    matrix_frame = page.frame_locator("#matrixViewer")
    expect(matrix_frame.locator("#sourceBar")).to_be_visible()
    expect(matrix_frame.locator('input[type="file"]')).to_have_count(0)
    expect(page.locator('input[type="file"][accept*="csv" i]')).to_have_count(0)
    expect(matrix_frame.locator("#matrix [data-port-key]")).to_have_count(0)
    page.locator("#queryTargets").fill(address + " 1")
    page.locator("#queryForm .primary").click()
    page.locator("#listView").click(timeout=90000)
    expect(matrix_frame.locator("#tbody")).to_be_visible()
    expect(matrix_frame.locator("#tbody")).to_contain_text("fixture-ap")
    expect(matrix_frame.locator("#matrix [data-port-key]")).to_have_attribute(
        "data-state", "normal"
    )
    expect(page.locator("#matrixViewer")).to_be_visible()
    expect(matrix_frame.locator("#tbody")).to_contain_text("8.1 W")
    matrix_frame.locator("#tbody").get_by_role("button", name=re.compile("상세 보기")).click()
    expect(matrix_frame.locator("#detail")).to_contain_text("fixture-ap")
    open_port_controls(page)
    page.locator("#refreshSwitch").click()
    expect(page.locator("#statusText")).to_contain_text("재조회 완료", timeout=90000)
    expect(page.locator("#matrixViewer")).to_be_visible()
    matrix_frame.locator("#status").select_option("off")
    expect(matrix_frame.locator("#tbody tr[data-port-key]")).to_have_count(0)
    matrix_frame.locator("#status").select_option("all")
    if shots:
        page.screenshot(path=shots / "03-live-port-query.png")
    expect(matrix_frame.locator("#tbody")).to_contain_text("fixture-ap", timeout=20000)
    page.locator("#portReviewTab").click()
    expect(matrix_frame.locator("#extraView")).to_be_visible()
    page.locator("#portMonitorTab").click()
    expect(page.locator("#matrixViewer")).to_be_visible()
    open_port_controls(page)
    page.locator("#selectEligible").click()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    with click_quick_poe(page, "poe_on", [(address, "1")]):
        expect(page.locator("#quickResult")).to_contain_text("이미 ON 1개", timeout=90000)
        expect(page.locator("#quickResult")).to_contain_text("사전 조회 전력")
        expect(page.locator("#quickResult tbody tr")).to_contain_text("8.1 W")
    assert switch.poe_enabled and "interface 1 power-over-ethernet" not in switch.commands
    for operation, expected in (("poe_off", False), ("poe_on", True)):
        page.locator("#operation").select_option(operation)
        page.locator("#selectEligible").click()
        expect(page.locator("#selectedCount")).to_have_text("선택 1개")
        assert switch.poe_enabled is not expected
        with click_quick_poe(page, operation, [(address, "1")]):
            expect(page.locator("#controlResult")).to_contain_text("검증 완료", timeout=90000)
        expect(page.locator("#controlResult")).to_contain_text(
            "링크 Up" if expected else "링크 Down"
        )
        expect_power_result(
            page, address, "미측정" if expected else "8.1 W", "8.1 W" if expected else "미측정"
        )
        assert switch.poe_enabled is expected
        expect(page.locator("#matrixViewer")).to_be_visible()
    page.locator("#operation").select_option("poe_off")
    page.locator("#selectEligible").click()
    page.locator("#preview").click()
    expect(page.locator("#review")).to_be_visible(timeout=90000)
    assert switch.poe_enabled
    submit = page.get_by_role("button", name="승인하고 명령 실행")
    expect(submit).to_be_disabled()
    phrase = page.locator("#reviewBody code").inner_text()
    page.locator("#reviewBody input").fill(phrase)
    submit.click()
    expect(page.locator("#controlResult")).to_contain_text("검증 완료", timeout=90000)
    assert not switch.poe_enabled
    expect_power_result(page, address, "8.1 W", "미측정")
    expect(page.locator("#matrixViewer")).to_be_visible()
    page.locator("#controlMode").select_option("single")
    page.locator("#singleIP").fill(address)
    page.locator("#singlePort").fill("1")
    page.locator("#inspectSingle").click()
    expect(page.locator("#singleControls + .output")).to_contain_text("PoE OFF", timeout=90000)
    page.locator("#operation").select_option("poe_on")
    page.locator("#preview").click()
    expect(page.locator("#review")).to_be_visible(timeout=90000)
    page.locator("#reviewBody input").fill(page.locator("#reviewBody code").inner_text())
    submit.click()
    expect(page.locator("#controlResult")).to_contain_text("검증 완료", timeout=90000)
    assert switch.poe_enabled
    expect_power_result(page, address, "미측정", "8.1 W")
    expect(page.locator('a[href="#history"], [data-page="history"]')).to_have_count(0)
    for retired in ("logs", "history"):
        nav("cable")
        page.evaluate("key => { location.hash = key; }", retired)
        page.wait_for_url("**/#ports")
        expect(page.locator('nav a[href="#ports"]')).to_have_attribute("aria-current", "page")
        expect(page.locator("#controlResult")).to_contain_text("검증 완료")
    nav("backup")
    expect(report).to_be_visible()
    page.locator("#backupResult").get_by_role("button", name="결과 폴더 열기").click()
    page.evaluate("location.hash = 'connect'")
    page.locator("#listKeys").click()
    expect(page.locator("#knownKeys")).to_contain_text("SHA256:")
    for endpoint in (address, second_address):
        row = page.locator("#knownKeys tbody tr").filter(has_text=endpoint)
        row.get_by_role("button", name="등록 삭제").click()
        page.get_by_role("button", name="이 키 삭제").click()
        expect(row).to_have_count(0)
    expect(page.locator("#knownKeys")).to_contain_text("등록된 호스트 키가 없습니다.")
    page.locator("#logout").click()
    expect(page.locator("#connectionState")).to_have_text("계정 미설정")
    nav("ports")
    edit_query_targets(page).fill(address + " 1")
    query_button = page.locator("#queryPorts")
    calls = []

    def record_job(request):
        if request.method == "POST" and request.url.endswith("/api/jobs"):
            calls.append(request.post_data_json["action"])

    page.on("request", record_job)
    query_button.click()
    dialog = page.get_by_role("dialog", name="SSH 접속 계정")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(query_button).to_be_focused()
    assert not calls
    query_button.click()
    enter_ssh_credentials(page, switch)
    expect(matrix.locator("#matrix [data-port-key]")).to_have_count(1, timeout=90000)
    assert calls == ["session.open", "ports.query"]
    page.remove_listener("request", record_job)
    assert len(opened) == 2 and len(opened[0]) == 32 and opened[0] == opened[1]
    assert not errors, errors
    page.locator("#shutdown").click()
    page.locator("#reviewBody button").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


def launch_test_browser(browser_type):
    if browser_type.name == "webkit":
        return browser_type.launch()
    chrome = os.environ.get(
        "ARUBA_WEB_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )
    return browser_type.launch(executable_path=chrome, headless=True)


def run_browser_case(case, *, switch_factory=MonitorFixture, browser_engine="chromium"):
    root = Path(os.environ.get("ARUBA_WEB_ROOT", Path(__file__).resolve().parents[1]))
    python = os.environ.get("ARUBA_WEB_PYTHON", sys.executable)
    shots = (
        Path(os.environ["ARUBA_WEB_SCREENSHOTS"]) if "ARUBA_WEB_SCREENSHOTS" in os.environ else None
    )
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory() as temp,
        switch_factory() as switch,
        MonitorFixture() as second_switch,
        sync_playwright() as playwright,
    ):
        folder = Path(temp)
        env = {**os.environ, "APPDATA": temp, "LOCALAPPDATA": temp, "USERPROFILE": temp}
        if python == sys.executable:
            env["PYTHONPATH"] = os.pathsep.join(
                filter(None, [site.getusersitepackages(), env.get("PYTHONPATH")])
            )
        process = subprocess.Popen(
            [
                python,
                "-B",
                "-X",
                "utf8",
                str(root / "server.py"),
                "--no-browser",
                "--port",
                "0",
                "--data-dir",
                temp,
            ],
            env=env,
        )
        try:
            for _ in range(300):
                if (folder / "server-info.json").is_file():
                    break
                if process.poll() is not None:
                    raise RuntimeError(f"Server exited: {process.returncode}")
                time.sleep(0.1)
            info = json.loads((folder / "server-info.json").read_text(encoding="utf-8"))
            browser = launch_test_browser(getattr(playwright, browser_engine))
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 960}, locale="ko-KR")
                page.set_default_timeout(15000)
                backup_directory = folder / "별도 백업 경로"
                backup_directory.mkdir()
                case(
                    page,
                    f"http://127.0.0.1:{info['port']}/start/{info['launch']}",
                    switch,
                    second_switch,
                    shots,
                    backup_directory,
                )
            finally:
                browser.close()
            assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def main():
    run_browser_case(exercise)
    print(
        "PASS Chrome UI: default ports page, three menus, themes, mobile layout, host trust, XLSX downloads, live matrix and review, confirmed batch/single SSH writes on loopback, retired routes, session close and shutdown."
    )


if __name__ == "__main__":
    main()

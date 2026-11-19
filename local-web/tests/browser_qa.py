"""Use installed Chrome and a loopback SSH fixture to exercise the local browser UI."""

import json
import os
import re
import site
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import expect, sync_playwright
from verify_http_ssh import MonitorFixture


def enter_ssh_credentials(page, switch):
    dialog = page.get_by_role("dialog", name="SSH 접속 계정", exact=True)
    expect(dialog).to_be_visible()
    dialog.locator("#queryUsername").fill(switch.username)
    dialog.locator("#queryPassword").fill(switch.password)
    dialog.get_by_role("button", name="계정 적용 후 계속").click()
    expect(dialog).to_be_hidden()
    expect(page.locator("#queryPassword")).to_have_value("")


@contextmanager
def click_quick_poe(page, operation, targets):
    """Verify one user click submits exactly one job for the selected SSH endpoints."""
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
            page.get_by_role("button", name=name, exact=True).click()
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
        for path in ("/logs", "/csv"):
            viewer.goto(urljoin(page.url, path))
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


def exercise(page, url, switch, second_switch, shots, backup_directory):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url)
    expect(page.locator("#version")).to_contain_text("v1.")
    logs = page.frame_locator("#logViewer")
    expect(logs.locator("#sourceSummary")).to_have_text("로그 미선택")
    expect(logs.locator("#logReport")).to_be_hidden()
    expect(logs.locator("#rows tr")).to_have_count(0)
    expect(logs.locator("#cards > *")).to_have_count(0)
    expect(logs.locator("#navAttention")).to_be_disabled()
    expect(logs.locator("#resetDemo")).to_have_count(0)
    if shots:
        page.screenshot(path=shots / "01-local-log-dashboard.png")

    def nav(key):
        page.locator(f'nav a[href="#{key}"]').click()
        expect(page.locator(f'[data-page="{key}"]')).to_be_visible()

    assert_theme_action(page, "dark")
    toggle = page.locator("#themeToggle")
    toggle.press("Space")
    assert_theme_action(page, "light")
    expect(logs.locator("html")).to_have_attribute("data-theme", "light")
    expect(logs.locator("#themeToggle")).to_be_hidden()
    page.set_viewport_size({"width": 390, "height": 844})
    assert_theme_action(page, "light")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if shots:
        page.screenshot(path=shots / "02-mobile-log-dashboard.png")

    # Opening the lazy CSV viewer after a switch and reloading should keep both viewers in sync.
    nav("ports")
    matrix = page.frame_locator("#csvViewer")
    expect(matrix.locator("html")).to_have_attribute("data-theme", "light")
    page.reload()
    assert_theme_action(page, "light")
    expect(matrix.locator("#themeToggle")).to_be_hidden()
    expect(matrix.locator("html")).to_have_attribute("data-theme", "light")
    toggle.press("Enter")
    assert_theme_action(page, "dark")
    for frame in (logs, matrix):
        expect(frame.locator("html")).to_have_attribute("data-theme", "dark")
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
    page.locator("#backupForm .primary").click()
    enter_ssh_credentials(page, switch)
    expect(page.locator('[data-page="backup"]')).to_be_visible()
    expect(page.locator("#backupResult")).to_contain_text("성공", timeout=90000)
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
    expect(page.get_by_role("link", name="포트 관제·제어")).to_have_count(1)
    expect(page.locator('nav a[href="#csv"]')).to_have_count(0)
    csv_frame = page.frame_locator("#csvViewer")
    expect(csv_frame.locator("#matrix [data-port-key]")).to_have_count(0)
    page.locator("#portLiveTab").click()
    page.locator("#queryTargets").fill(address + " 1")
    page.locator("#queryForm .primary").click()
    expect(csv_frame.locator("#tbody")).to_be_visible(timeout=90000)
    expect(csv_frame.locator("#tbody")).to_contain_text("fixture-ap")
    expect(csv_frame.locator("#matrix [data-port-key]")).to_have_attribute("data-state", "normal")
    expect(page.locator("#csvViewer")).to_be_visible()
    expect(csv_frame.locator("#tbody")).to_contain_text("8.1 W")
    csv_frame.locator("#tbody").get_by_role("button", name=re.compile("상세 보기")).click()
    expect(csv_frame.locator("#detail")).to_contain_text("fixture-ap")
    page.locator("#refreshSwitch").click()
    expect(page.locator("#statusText")).to_contain_text("재조회 완료", timeout=90000)
    expect(page.locator("#csvViewer")).to_be_visible()
    csv_frame.locator("#status").select_option("off")
    expect(csv_frame.locator("#tbody tr[data-port-key]")).to_have_count(0)
    csv_frame.locator("#status").select_option("all")
    with page.expect_download() as output:
        page.locator('#queryFiles a[download$=".csv"]').click()
    csv_data = output.value.path().read_bytes()
    assert "fixture-ap" in csv_data.decode("utf-8-sig")
    if shots:
        page.screenshot(path=shots / "03-live-port-query.png")
    expect(csv_frame.locator("#tbody")).to_contain_text("fixture-ap", timeout=20000)
    page.locator("#portReviewTab").click()
    expect(csv_frame.locator("#extraView")).to_be_visible()
    page.locator("#portMonitorTab").click()
    with page.expect_file_chooser() as comparison_event:
        page.locator("#portCompareTab").click()
    comparison_event.value.set_files(
        {"name": "earlier.csv", "mimeType": "text/csv", "buffer": csv_data}
    )
    expect(csv_frame.locator("h1")).to_contain_text("비교")
    expect(csv_frame.locator("#extraView")).to_contain_text("변화 목록")
    page.locator("#showMatrix").click()
    expect(page.locator("#csvViewer")).to_be_visible()
    page.locator("#selectEligible").click()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    with click_quick_poe(page, "poe_on", [(address, "1")]):
        expect(page.locator("#quickResult")).to_contain_text("이미 ON 1개", timeout=90000)
    assert switch.poe_enabled and "interface 1 power-over-ethernet" not in switch.commands
    for operation, expected in (("poe_off", False), ("poe_on", True)):
        page.locator("#operation").select_option(operation)
        page.locator("#selectEligible").click()
        assert switch.poe_enabled is not expected
        with click_quick_poe(page, operation, [(address, "1")]):
            expect(page.locator("#controlResult")).to_contain_text("검증 완료", timeout=90000)
        expect(page.locator("#controlResult")).to_contain_text(
            "링크 Up" if expected else "링크 Down"
        )
        assert switch.poe_enabled is expected
        page.locator("#refreshSwitch").click()
        expect(page.locator("#statusText")).to_contain_text("재조회 완료", timeout=90000)
        expect(page.locator("#csvViewer")).to_be_visible()
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
    page.locator("#refreshPorts").click()
    expect(page.locator("#statusText")).to_contain_text("재조회 완료", timeout=90000)
    expect(page.locator("#csvViewer")).to_be_visible()
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
    nav("logs")
    page.locator("#sshLogs summary").click()
    page.locator("#logTargets").fill(address + "\n" + second_address)
    page.locator("#logForm button").click()
    expect(page.locator("#logResults")).to_contain_text("성공", timeout=90000)
    expect(logs.locator("#sourceSummary")).to_contain_text("edge-lab")
    for endpoint in (second_address, address):
        page.locator("#logResults tbody tr").filter(has_text=endpoint).get_by_role(
            "button", name="이 로그 보기"
        ).click()
        expect(logs.locator("#sourceSummary")).to_contain_text(endpoint)
        expect(logs.locator("#logReport")).to_be_visible()
        for state in ("off", "down", "up"):
            expect(logs.locator(f'#rows tr[data-status="{state}"]')).to_have_count(1)
            expect(logs.locator("#count-" + state)).to_have_text("1")
    logs.locator("#navAttention").click()
    expect(logs.locator("#rows tr")).to_have_count(2)
    logs.locator("#navAll").click()
    nav("history")
    expect(page.locator("#historyTable")).to_contain_text("구성 백업")
    page.locator("#historyTable").get_by_role("button", name="결과 폴더 열기").first.click()
    expect(page.locator("#controlHistory")).to_be_visible()
    page.locator("#controlHistory").click()
    expect(page.locator("#controlHistoryTable")).to_contain_text("검증 완료")
    old_header = {
        "event": "plan",
        "created_at": "2026-11-18",
        "action": "poe_off",
        "items": [{"endpoint": address, "interface": "1", "hostname": "old-fixture"}],
    }
    saved = json.dumps(old_header) + "\n" + json.dumps({"event": "intent", "index": 0})
    page.locator("#importControlHistory + input").set_input_files(
        {"name": "saved-batch.jsonl", "mimeType": "application/json", "buffer": saved.encode()}
    )
    expect(page.locator("#controlHistoryTable")).to_contain_text("결과 미기록")
    expect(page.locator("#controlHistoryTable")).to_contain_text("old-fixture")
    page.locator("#backupHistory").click()
    expect(page.locator("#backupHistoryTable")).to_contain_text("result.xlsx")
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
    nav("logs")
    page.locator("#logTargets").fill(address)
    log_button = page.get_by_role("button", name="로그 수집")
    calls = []

    def record_job(request):
        if request.method == "POST" and request.url.endswith("/api/jobs"):
            calls.append(request.post_data_json["action"])

    page.on("request", record_job)
    log_button.click()
    dialog = page.get_by_role("dialog", name="SSH 접속 계정")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(log_button).to_be_focused()
    assert not calls
    log_button.click()
    enter_ssh_credentials(page, switch)
    expect(page.locator("#statusText")).to_contain_text("로그 수집 완료", timeout=90000)
    assert calls == ["session.open", "logs.query"]
    page.remove_listener("request", record_job)
    assert len(opened) == 2 and len(opened[0]) == 32 and opened[0] == opened[1]
    assert not errors, errors
    page.locator("#shutdown").click()
    page.locator("#reviewBody button").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


def run_browser_case(case, *, switch_factory=MonitorFixture):
    root = Path(os.environ.get("ARUBA_WEB_ROOT", Path(__file__).resolve().parents[1]))
    python = os.environ.get("ARUBA_WEB_PYTHON", sys.executable)
    chrome = os.environ.get(
        "ARUBA_WEB_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )
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
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
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
        "PASS Chrome UI: local launch, three log states, themes, mobile layout, host trust, XLSX and CSV downloads, live matrix, CSV comparison, confirmed batch/single SSH writes on loopback, live logs, history, session close and shutdown."
    )


if __name__ == "__main__":
    main()

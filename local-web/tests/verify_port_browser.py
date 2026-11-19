"""Chrome regression for offline editing, matrix and the normal loopback SSH query entrypoint."""

import json
import os
import site
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from verify_http_ssh import MonitorFixture


def query_with_inline_credentials(page, switch):
    targets = page.locator("#queryTargets")
    original_targets = targets.input_value()
    submit = page.get_by_role("button", name="포트 조회", exact=True)
    dialog = page.get_by_role("dialog", name="SSH 접속 계정")
    actions = []

    def record_action(request):
        if request.method == "POST" and request.url.endswith("/api/jobs"):
            actions.append(request.post_data_json["action"])

    page.on("request", record_action)
    expect(submit).to_be_enabled()
    for blank in ("", "   "):
        targets.fill(blank)
        submit.click()
        expect(dialog).to_be_hidden()
        expect(targets).to_be_focused()
    targets.fill(original_targets)

    for cancel in ("button", "escape"):
        submit.click()
        expect(dialog).to_be_visible()
        expect(page.locator("#queryUsername")).to_be_focused()
        page.locator("#queryPassword").fill(switch.password)
        page.locator("#queryEnableSecret").fill(switch.password)
        if cancel == "button":
            dialog.get_by_role("button", name="취소").click()
        else:
            page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(page.locator("#queryPassword")).to_have_value("")
        expect(page.locator("#queryEnableSecret")).to_have_value("")
        expect(targets).to_have_value(original_targets)
        expect(submit).to_be_focused()
    # A close event may be delivered after the next task has already opened the prompt.
    submit.click()
    page.locator("#queryUsername").fill("cancelled-user")
    page.locator("#queryPassword").fill("cancelled-password")
    page.locator("#queryEnableSecret").fill("cancelled-enable")
    page.evaluate("""() => new Promise((resolve, reject) => {
        const prompt = document.getElementById('queryCredentials');
        const timeout = setTimeout(() => reject(new Error('Missing close event')), 3000);
        prompt.addEventListener('close', () => { clearTimeout(timeout); resolve(); }, {once: true});
        document.getElementById('cancelQueryCredentials').click();
        document.getElementById('queryPorts').click();
    })""")
    expect(dialog).to_be_visible()
    expect(page.locator("#queryUsername")).to_be_focused()
    for field in ("queryUsername", "queryPassword", "queryEnableSecret"):
        expect(page.locator("#" + field)).to_have_value("")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    assert not actions and not switch.auth_attempts

    submit.click()
    page.locator("#queryUsername").fill(switch.username)
    accept = dialog.get_by_role("button", name="계정 적용 후 계속")
    accept.click()
    expect(dialog).to_be_visible()
    expect(page.locator("#queryPassword")).to_be_focused()
    assert not actions
    error = "계정 적용 요청에 실패했습니다."
    page.route(
        "**/api/jobs", lambda route: route.fulfill(status=409, json={"error": error}), times=1
    )
    page.locator("#queryPassword").fill(switch.password)
    accept.click()
    expect(dialog.get_by_role("alert")).to_contain_text(error)
    expect(page.locator("#queryPassword")).to_have_value("")
    expect(targets).to_have_value(original_targets)
    assert actions == ["session.open"] and not switch.auth_attempts

    page.locator("#queryPassword").fill(switch.password)
    page.locator("#queryPassword").press("Enter")

    expect(page.frame_locator("#csvViewer").locator("#tbody")).to_contain_text(
        "8.1 W", timeout=45000
    )
    expect(page.locator("#queryPassword")).to_have_value("")
    assert switch.auth_attempts
    assert actions == ["session.open", "session.open", "ports.query"]
    expect(page.locator('a[href="#connect"]')).to_have_count(0)
    expect(page.locator('[data-page="connect"]')).to_be_hidden()

    page.locator("#portLiveTab").click()
    submit.click()
    expect(page.locator("#statusText")).to_contain_text("포트 조회 완료", timeout=45000)
    expect(dialog).to_be_hidden()
    expect(targets).to_have_value(original_targets)
    assert actions == ["session.open", "session.open", "ports.query", "ports.query"]
    page.remove_listener("request", record_action)


def exercise(page, switch):
    issues = []
    page.on("pageerror", lambda error: issues.append(str(error)))
    page.locator('nav a[href="#ports"]').click()
    page.locator("#portLiveTab").click()
    page.locator("#targetRows").click()
    page.locator('#editTargetRows input[data-field="ip"]').fill("127.0.0.1")
    page.locator('#editTargetRows input[data-field="interfaces"]').fill("1")
    page.locator("#advancedTargets").check()
    page.locator('#editTargetRows input[data-field="ssh_port"]').fill(str(switch.port))
    page.locator('#editTargetRows input[data-field="label"]').fill("교육동")
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetPreview")).to_contain_text("교육동")
    page.locator("#acceptTargets").click()
    page.locator("#portMonitorTab").click()
    expect(page.locator(".matrix-tile")).to_have_count(1)
    expect(page.locator(".matrix-tile")).to_have_attribute("data-state", "unclassified")
    expect(page.locator(".matrix-tile")).to_have_attribute("data-phase", "pending")
    with page.expect_file_chooser() as picker:
        page.locator("#portCompareTab").click()
    picker.value.set_files(
        {"name": "invalid.csv", "mimeType": "text/csv", "buffer": b"other,columns\na,b"}
    )
    expect(page.locator("#statusText")).to_contain_text("invalid.csv")
    expect(page.locator("#csvViewer")).to_be_hidden()
    expect(page.locator(".matrix-tile")).to_be_visible()
    page.locator("#matrixSize").select_option("large")
    expect(page.locator("#matrixGroups")).to_have_attribute("data-zoom", "large")
    source = page.locator("#queryTargets").input_value()
    bulk = "\n".join(f"192.0.{n // 250 + 1}.{n % 250 + 1} 1-48" for n in range(300))
    page.locator("#portLiveTab").click()
    page.locator("#queryTargets").fill(bulk)
    page.locator("#targetVerify").click()
    expect(page.locator("#targetMessage")).to_contain_text("300대 · 14400개")
    page.locator("#acceptTargets").click()
    page.locator("#portMonitorTab").click()
    expect(page.locator(".matrix-tile")).to_have_count(192)
    page.locator("#matrixNext").click()
    expect(page.locator("#matrixPage")).to_have_text("2 / 75")
    page.locator("#portLiveTab").click()
    page.locator("#queryTargets").fill(source)
    page.locator("#targetVerify").click()
    expect(page.locator("#targetMessage")).to_contain_text("1대 · 1개")
    page.locator("#acceptTargets").click()
    page.locator("#portMonitorTab").click()
    expect(page.locator(".matrix-tile")).to_have_count(1)
    page.locator("#portLiveTab").click()
    page.locator("#targetPaste").click()
    page.locator("#pasteMode").select_option("append")
    page.locator("#pasteTargets").fill("192.0.2.2\t1/1,1/2\n192.0.2.2\t1/2")
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetMessage")).to_contain_text("2대 · 3개")
    page.locator("#acceptTargets").click()
    page.locator("#portMonitorTab").click()
    expect(page.locator(".matrix-tile")).to_have_count(3)
    page.locator("[data-check-group='192.0.2.2']").check()
    expect(page.locator("#selectedCount")).to_contain_text("선택 2개")
    page.locator("#inventorySearch").fill("교육동")
    expect(page.locator(".matrix-tile")).to_have_count(1)
    page.locator("#inventorySearch").fill("")
    page.locator("#portLiveTab").click()
    page.locator("#targetPaste").click()
    page.locator("#pasteTargets").fill("bad\t99")
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetMessage")).not_to_have_text("")
    expect(page.locator("#acceptTargets")).to_be_disabled()
    page.locator("#targetClose").click()
    page.locator("#portMonitorTab").click()
    expect(page.locator(".matrix-tile")).to_have_count(3)
    page.locator("#portLiveTab").click()
    page.locator("#targetRows").click()
    page.locator("#editTargetRows tr").filter(
        has=page.locator('input[data-field="ip"]')
    ).last.get_by_role("button", name="삭제").click()
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetMessage")).to_contain_text("1대 · 1개")
    page.locator("#acceptTargets").click()
    page.locator("#portMonitorTab").click()
    expect(page.locator(".matrix-tile")).to_have_count(1)
    expect(page.locator("#selectedCount")).to_contain_text("선택 0개")
    page.locator("#portLiveTab").click()
    query_with_inline_credentials(page, switch)
    csv_view = page.frame_locator("#csvViewer")
    tile = csv_view.locator("#matrix [data-port-key]")
    expect(tile).to_have_attribute("data-state", "normal")
    tile.click()
    detail = csv_view.locator("#detail")
    for fact in ("8.1 W", "System Name", "JL255A", "WC.16.11.0025"):
        expect(detail).to_contain_text(fact)
    page.locator("#portReviewTab").click()
    expect(csv_view.locator("#extraView .dr-list button")).to_have_count(0)
    page.locator("#portMonitorTab").click()
    tile.click()
    page.locator("#refreshPorts").click()
    expect(page.locator("#statusText")).to_contain_text("재조회 완료", timeout=45000)
    assert not issues, issues


def main():
    root = Path(os.environ.get("ARUBA_WEB_ROOT", Path(__file__).resolve().parents[1]))
    python = os.environ.get("ARUBA_WEB_PYTHON", sys.executable)
    chrome = os.environ.get(
        "ARUBA_WEB_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )
    with (
        tempfile.TemporaryDirectory() as temp,
        MonitorFixture() as switch,
        sync_playwright() as playwright,
    ):
        folder = Path(temp)
        env = {**os.environ, "APPDATA": temp, "LOCALAPPDATA": temp, "USERPROFILE": temp}
        if python == sys.executable:
            env["PYTHONPATH"] = os.pathsep.join(
                filter(None, [site.getusersitepackages(), env.get("PYTHONPATH")])
            )
        proc = subprocess.Popen(
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
                if proc.poll() is not None:
                    raise RuntimeError("server exited")
                time.sleep(0.1)
            info = json.loads((folder / "server-info.json").read_text(encoding="utf-8"))
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900}, locale="ko-KR")
                page.set_default_timeout(10000)
                page.goto(f"http://127.0.0.1:{info['port']}/start/{info['launch']}")
                exercise(page, switch)
            finally:
                browser.close()
        finally:
            proc.terminate()
            proc.wait(timeout=15)
    print(
        "PASS target editor, matrix, inline SSH credentials, cancel, host trust, query and refresh"
    )


if __name__ == "__main__":
    main()

"""Chrome regression for offline editing, matrix and the normal loopback SSH query entrypoint."""

import json
import os
import site
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from browser_qa import expect_hidden_selection, open_port_controls, open_target_editor, power_filter
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

    expect(page.frame_locator("#matrixViewer").locator("#tbody")).to_contain_text(
        "8.1 W", timeout=45000
    )
    expect(page.locator("#queryPassword")).to_have_value("")
    assert switch.auth_attempts
    assert actions == ["session.open", "session.open", "ports.query"]
    expect(page.locator('a[href="#connect"]')).to_have_count(0)
    expect(page.locator('[data-page="connect"]')).to_be_hidden()

    page.get_by_role("button", name="전체 재조회", exact=True).click()
    expect(page.locator("#statusText")).to_contain_text("포트 조회 완료", timeout=45000)
    expect(dialog).to_be_hidden()
    expect(targets).to_have_value(original_targets)
    assert actions == ["session.open", "session.open", "ports.query", "ports.query"]
    page.remove_listener("request", record_action)


def exercise(page, switch):
    issues = []
    page.on("pageerror", lambda error: issues.append(str(error)))
    page.locator('nav a[href="#ports"]').click()
    expect(page.locator("#queryForm")).to_be_visible()
    expect(page.locator("#manageTargets")).to_be_visible()
    expect(page.locator("#targetOptions, #targetRows, #targetPaste, #targetVerify")).to_have_count(
        0
    )
    open_target_editor(page, "rows")
    page.locator('#editTargetRows input[data-field="ip"]').fill("127.0.0.1")
    page.locator('#editTargetRows input[data-field="interfaces"]').fill("1")
    page.locator("#advancedTargets").check()
    page.locator('#editTargetRows input[data-field="ssh_port"]').fill(str(switch.port))
    page.locator('#editTargetRows input[data-field="label"]').fill("교육동")
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetPreview")).to_contain_text("교육동")
    page.locator("#acceptTargets").click()
    expect(page.locator(".matrix-tile")).to_have_count(1)
    expect(page.locator(".matrix-tile")).to_have_attribute("data-state", "unclassified")
    expect(page.locator(".matrix-tile")).to_have_attribute("data-phase", "pending")
    expect(page.locator(".matrix-tile .tile-link")).to_have_text("—")
    expect(page.locator("#matrixViewer")).to_be_hidden()
    expect(page.locator(".matrix-tile")).to_be_visible()
    page.locator("#matrixSize").select_option("large")
    expect(page.locator("#matrixGroups")).to_have_attribute("data-zoom", "large")
    source = page.locator("#queryTargets").input_value()
    open_target_editor(page)
    draft = page.locator("#editorTargets")
    expect(draft).to_have_value(source)
    draft.fill("192.0.2.45 1")
    page.locator("#verifyEditor").click()
    expect(page.locator("#acceptTargets")).to_be_enabled()
    draft.fill("192.0.2.46 2")
    expect(page.locator("#acceptTargets")).to_be_disabled()
    page.locator("#targetClose").click()
    expect(page.locator("#queryTargets")).to_have_value(source)
    expect(page.locator(".matrix-tile")).to_have_count(1)
    # File imports operate on the editor draft until the operator reviews and applies it.
    open_target_editor(page)
    page.locator("#exportProfile").click()
    saved_profile = page.locator("#targetEditor #profileFiles a").first
    expect(saved_profile).to_be_visible()
    with page.expect_download() as download:
        saved_profile.click()
    profile = Path(download.value.path()).read_bytes()
    importer = page.locator("#targetEditor [data-import=queryTargets]")
    candidate = "192.0.2.45 2-3"
    for apply in (False, True):
        with page.expect_file_chooser() as picker:
            importer.click()
        picker.value.set_files(
            {"name": "targets.txt", "mimeType": "text/plain", "buffer": candidate.encode()}
        )
        expect(draft).to_have_value(candidate)
        expect(page.locator("#queryTargets")).to_have_value(source)
        expect(page.locator(".matrix-tile")).to_have_count(1)
        if apply:
            page.locator("#verifyEditor").click()
            page.locator("#acceptTargets").click()
        else:
            page.locator("#targetClose").click()
            expect(page.locator("#queryTargets")).to_have_value(source)
            open_target_editor(page)
    expect(page.locator(".matrix-tile")).to_have_count(2)
    applied_text = page.locator("#queryTargets").input_value()
    open_target_editor(page)
    with page.expect_file_chooser() as picker:
        importer.click()
    picker.value.set_files(
        {"name": "targets.json", "mimeType": "application/json", "buffer": profile}
    )
    expect(draft).to_have_value(source)
    expect(page.locator("#queryTargets")).to_have_value(applied_text)
    expect(page.locator(".matrix-tile")).to_have_count(2)
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetPreview")).to_contain_text("교육동")
    expect(page.locator("#targetPreview")).to_contain_text(str(switch.port))
    page.locator("#acceptTargets").click()
    expect(page.locator("#targetEditor")).to_be_hidden()
    expect(page.locator("#queryTargets")).to_have_value(source)
    expect(page.locator(".matrix-tile")).to_have_count(1)
    bulk = "\n".join(f"192.0.{n // 250 + 1}.{n % 250 + 1} 1-48" for n in range(300))
    page.locator("#queryTargets").fill(bulk)
    open_target_editor(page)
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetMessage")).to_contain_text("300대 · 14400개")
    page.locator("#acceptTargets").click()
    expect(page.locator(".matrix-tile")).to_have_count(192)
    page.locator("#matrixNext").click()
    expect(page.locator("#matrixPage")).to_have_text("2 / 75")
    # A long registration remains operable inside its pane without moving the workspace.
    page.locator(".matrix-tile").last.click()
    expect(page.locator("#matrixDetails")).to_be_visible()
    expect(page.locator(".matrix-tile").last).to_be_focused()
    expect(page.locator("#matrixDetails input")).to_be_checked()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    select_all = page.locator("#matrixSelectAll")
    expect(select_all).to_have_accessible_name("전체 선택")
    select_all.click()
    expect(page.locator("#selectedCount")).to_have_text("선택 14400개")
    expect_hidden_selection(page, 14400 - 192)
    expect(select_all).to_have_accessible_name("전체 선택 해제")
    select_all.click()
    expect(page.locator("#selectedCount")).to_be_hidden()
    expect(page.locator("#matrixDetails input")).not_to_be_checked()
    assert page.evaluate("scrollY === 0 && document.documentElement.scrollWidth <= innerWidth")
    page.locator("#queryTargets").fill(source)
    open_target_editor(page)
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetMessage")).to_contain_text("1대 · 1개")
    page.locator("#acceptTargets").click()
    expect(page.locator(".matrix-tile")).to_have_count(1)
    open_target_editor(page, "paste")
    page.locator("#pasteMode").select_option("append")
    page.locator("#pasteTargets").fill("192.0.2.2\t1/1,1/2\n192.0.2.2\t1/2")
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetMessage")).to_contain_text("2대 · 3개")
    page.locator("#acceptTargets").click()
    expect(page.locator(".matrix-tile")).to_have_count(3)
    page.locator("[data-check-group='192.0.2.2']").check()
    expect(page.locator("#selectedCount")).to_contain_text("선택 2개")
    page.locator("#inventorySearch").fill("교육동")
    expect(page.locator(".matrix-tile")).to_have_count(1)
    expect_hidden_selection(page, 2)
    # A broader scope stays visible on a narrow screen, even after closing options.
    viewport = page.viewport_size
    page.set_viewport_size({"width": 320, "height": 720})
    open_port_controls(page)
    scope = page.locator("#selectionScope")
    scope.select_option("all")
    page.locator("#controlHeading").click()
    scope_label = page.locator("#matrixSelectionScope")
    expect(scope_label).to_be_visible()
    expect(scope_label).to_have_text("등록된 전체")
    assert select_all.bounding_box()["height"] >= 44
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    select_all.click()
    expect(page.locator("#selectedCount")).to_have_text("선택 3개")
    open_port_controls(page)
    scope.select_option("filtered")
    page.locator("#controlHeading").click()
    expect(scope_label).to_be_hidden()
    select_all.click()
    expect(page.locator("#selectedCount")).to_have_text("선택 2개")
    page.set_viewport_size(viewport)
    page.locator("#inventorySearch").fill("")
    open_target_editor(page, "paste")
    page.locator("#pasteTargets").fill("bad\t99")
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetMessage")).not_to_have_text("")
    expect(page.locator("#acceptTargets")).to_be_disabled()
    page.locator("#targetClose").click()
    expect(page.locator(".matrix-tile")).to_have_count(3)
    open_target_editor(page, "rows")
    page.locator("#editTargetRows tr").filter(
        has=page.locator('input[data-field="ip"]')
    ).last.get_by_role("button", name="삭제").click()
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetMessage")).to_contain_text("1대 · 1개")
    page.locator("#acceptTargets").click()
    expect(page.locator(".matrix-tile")).to_have_count(1)
    expect(page.locator("#selectedCount")).to_contain_text("선택 0개")
    expect(page.locator("#queryTargets")).to_be_editable()
    query_with_inline_credentials(page, switch)
    matrix_view = page.frame_locator("#matrixViewer")
    tile = matrix_view.locator("#matrix [data-port-key]")
    expect(tile).to_have_attribute("data-state", "normal")
    tile.click()
    detail = matrix_view.locator("#detail")
    for fact in ("8.1 W", "System Name", "JL255A", "WC.16.11.0025"):
        expect(detail).to_contain_text(fact)
    normal = power_filter(matrix_view, "normal")
    normal.click()
    expect(normal).to_have_attribute("aria-pressed", "true")
    expect(tile).to_have_attribute("aria-pressed", "true")
    expect_hidden_selection(page, 0)
    normal.click()
    open_port_controls(page)
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

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
    expect(page.locator(".matrix-tile")).to_have_count(1)
    expect(page.locator(".matrix-tile")).to_have_attribute("data-state", "pending")
    source = page.locator("#queryTargets").input_value()
    bulk = "\n".join(f"192.0.{n // 250 + 1}.{n % 250 + 1} 1-48" for n in range(300))
    page.locator("#queryTargets").fill(bulk)
    page.locator("#targetVerify").click()
    expect(page.locator("#targetMessage")).to_contain_text("300대 · 14400개")
    page.locator("#acceptTargets").click()
    expect(page.locator(".matrix-tile")).to_have_count(192)
    page.locator("#matrixNext").click()
    expect(page.locator("#matrixPage")).to_have_text("2 / 75")
    page.locator("#queryTargets").fill(source)
    page.locator("#targetVerify").click()
    expect(page.locator("#targetMessage")).to_contain_text("1대 · 1개")
    page.locator("#acceptTargets").click()
    expect(page.locator(".matrix-tile")).to_have_count(1)
    page.locator("#targetPaste").click()
    page.locator("#pasteMode").select_option("append")
    page.locator("#pasteTargets").fill("192.0.2.2\t1/1,1/2\n192.0.2.2\t1/2")
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetMessage")).to_contain_text("2대 · 3개")
    page.locator("#acceptTargets").click()
    expect(page.locator(".matrix-tile")).to_have_count(3)
    page.locator("[data-check-group='192.0.2.2']").check()
    expect(page.locator("#selectedCount")).to_contain_text("선택 2개")
    page.locator("#portSearch").fill("교육동")
    expect(page.locator(".matrix-tile")).to_have_count(1)
    page.locator("#portSearch").fill("")
    page.locator("#targetPaste").click()
    page.locator("#pasteTargets").fill("bad\t99")
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetMessage")).not_to_have_text("")
    expect(page.locator("#acceptTargets")).to_be_disabled()
    page.locator("#targetClose").click()
    expect(page.locator(".matrix-tile")).to_have_count(3)
    page.locator("#targetRows").click()
    page.locator("#editTargetRows tr").filter(
        has=page.locator('input[data-field="ip"]')
    ).last.get_by_role("button", name="삭제").click()
    page.locator("#verifyEditor").click()
    expect(page.locator("#targetMessage")).to_contain_text("1대 · 1개")
    page.locator("#acceptTargets").click()
    expect(page.locator(".matrix-tile")).to_have_count(1)
    expect(page.locator("#selectedCount")).to_contain_text("선택 0개")
    page.locator('nav a[href="#connect"]').click()
    page.locator("#username").fill(switch.username)
    page.locator("#password").fill(switch.password)
    page.locator("#loginForm .primary").click()
    expect(page.locator("#connectionState")).to_contain_text(switch.username)
    page.locator("#trustTargets").fill(f"127.0.0.1:{switch.port}")
    page.locator("#inspectForm button").click()
    expect(page.locator("#trustResult")).to_contain_text("SHA256:")
    page.locator("#trustResult").get_by_role("button", name="지문 검토").click()
    page.get_by_role("button", name="이 지문 승인").click()
    expect(page.locator("#trustResult")).to_contain_text("등록된 키")
    page.locator('nav a[href="#ports"]').click()
    page.locator("#portLiveTab").click()
    page.locator("#queryForm .primary").click()
    expect(page.locator("#portTable")).to_contain_text("8.1 W", timeout=45000)
    page.locator("#portLiveTab").click()
    tile = page.locator(".matrix-tile")
    expect(tile).to_have_count(1)
    expect(tile).to_have_attribute("data-state", "normal")
    tile.click()
    expect(page.locator("#matrixDetails")).to_contain_text("8.1 W")
    page.locator("#portControlTab").click()
    expect(page.locator("#rowDetails")).to_be_visible()
    expect(page.locator("#rowDetails")).to_contain_text("System Name (장비 직접 조회)")
    expect(page.locator("#rowDetails")).to_contain_text("JL255A")
    expect(page.locator("#rowDetails")).to_contain_text("WC.16.11.0025")
    page.locator("#portLiveTab").click()
    page.locator("#matrixSize").select_option("large")
    expect(page.locator("#matrixGroups")).to_have_attribute("data-zoom", "large")
    page.locator("#matrixPriority").click()
    expect(page.locator("#matrixPriority")).to_have_attribute("aria-pressed", "true")
    page.locator("#matrixPriority").click()
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
        "PASS target row editor, Excel append/replace validation, matrix, selection, live SSH and targeted refresh"
    )


if __name__ == "__main__":
    main()

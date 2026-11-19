"""Exercise backup-specific desktop parity in Chrome with one loopback SSH service."""

import json
import os
import secrets
import site
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from verify_http_ssh import Http, MonitorFixture


def exercise(page, http, url, switch, folder):
    endpoint = "127.0.0.1:" + str(switch.port)
    (check,) = http.job("hostkey.inspect", targets=endpoint)["checks"]
    http.job("hostkey.approve", endpoint=endpoint, fingerprint=check["fingerprint"])
    invalid_password = secrets.token_urlsafe()
    http.job("session.open", username=switch.username, password=invalid_password)
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.goto(url)
    page.locator("nav a[href='#backup']").click()
    page.locator("#backupTargets").fill("127.0.0.1")
    page.locator("#backupSSHPort").fill(str(switch.port))
    page.locator("#attempts").fill("1")
    folder = folder / "추가 백업"
    page.locator("#backupDirectory").fill(str(folder))
    page.locator("#backupForm .primary").click()
    expect(page.locator("#backupDiagnostics")).to_be_visible(timeout=90000)
    text = page.locator("#backupDiagnostics pre").text_content()
    assert text.startswith("A3F1-") and text.endswith(" \N{MULTIPLICATION SIGN} 1")
    assert "127.0.0.1" not in text and switch.username not in text
    expect(page.locator("#backupResult")).to_contain_text("지문 시도")
    expect(page.locator("#backupResult")).to_contain_text("백업 시도")
    expect(page.locator("#backupResult")).to_contain_text("기타 실패 1대")
    assert list(folder.glob("*/*/result.xlsx"))
    page.locator("nav a[href='#history']").click()
    page.locator("#backupHistory").click()
    expect(page.locator("#backupHistoryTable")).to_contain_text("지정한 백업 폴더")
    expect(
        page.locator("#backupHistoryTable").get_by_role("button", name="파일 위치 열기").first
    ).to_be_visible()

    page.locator("nav a[href='#backup']").click()
    page.locator("#backupTargets").fill(endpoint + " " + invalid_password)
    page.locator("#uiInspectorOpen").click()
    page.get_by_role("button", name="요소 선택", exact=True).click()
    page.locator("#backupTargets").click()
    expect(page.locator("#uiInspectorDialog")).to_be_visible()
    expect(
        page.locator("#uiInspectorDialog option").filter(has_text="SSH 접속 설정")
    ).to_have_count(0)
    text = page.locator("#uiRequestPreview").inner_text()
    assert (
        "UI 식별자: backupTargets" in text
        and invalid_password not in text
        and "127.0.0.1" not in text
    )
    page.locator("#uiInspectorDialog select").select_option(label="구성 백업 · 기본 SSH 포트")
    expect(page.locator("#uiRequestPreview")).to_contain_text("backupSSHPort")
    page.get_by_role("button", name="작업 요청 복사").click()
    assert "backupSSHPort" in page.evaluate("navigator.clipboard.readText()")
    page.locator("#uiInspectorDialog").get_by_role("button", name="닫기").click()
    page.get_by_role("button", name="식별 종료").click()
    expect(page.locator("#uiInspectorDock")).to_be_hidden()
    assert not errors, errors


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
        env = dict(os.environ, LOCALAPPDATA=temp, APPDATA=temp, USERPROFILE=temp)
        if python == sys.executable:
            env["PYTHONPATH"] = os.pathsep.join(
                filter(None, [site.getusersitepackages(), env.get("PYTHONPATH")])
            )
        with subprocess.Popen(
            [
                python,
                "-B",
                "-X",
                "utf8",
                str(root / "server.py"),
                "--no-browser",
                "--data-dir",
                temp,
                "--port",
                "0",
            ],
            env=env,
        ) as child:
            try:
                info_path = Path(temp) / "server-info.json"
                for _ in range(300):
                    if info_path.is_file():
                        break
                    if child.poll() is not None:
                        raise RuntimeError("Server exited")
                    time.sleep(0.1)
                info = json.loads(info_path.read_text(encoding="utf-8"))
                http = Http(info["port"], info["launch"])
                browser = playwright.chromium.launch(executable_path=chrome, headless=True)
                try:
                    context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
                    exercise(
                        context.new_page(),
                        http,
                        http.base + "/start/" + info["launch"],
                        switch,
                        Path(temp),
                    )
                finally:
                    browser.close()
                http.post("/api/shutdown", {})
                assert child.wait(10) == 0
            finally:
                if child.poll() is None:
                    child.terminate()
    print(
        "PASS Chrome backup parity: common port, diagnostics, saved report locations, UI inspector and static clipboard."
    )


if __name__ == "__main__":
    main()

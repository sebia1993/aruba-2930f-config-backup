"""Verify IP-only config backup using a real port-22 loopback SSH service and local browsers."""

import json
import os
import secrets
import site
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from browser_qa import enter_ssh_credentials, launch_test_browser
from playwright.sync_api import expect, sync_playwright
from verify_http_ssh import Http, backup_switch


def exercise(page, http, url, switch, folder, *, touch):
    endpoint = f"{switch.ip}:{switch.port}"
    assert switch.port == 22
    jobs = []

    def record(request):
        if request.method == "POST" and request.url.endswith("/api/jobs"):
            jobs.append(request.post_data_json)

    page.on("request", record)
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.goto(url)
    label = "스위치 컨피그 백업"
    link = page.get_by_role("link", name=label, exact=True)
    link.tap() if touch else link.click()
    expect(page.get_by_role("heading", level=1, name=label, exact=True)).to_be_visible()
    expect(page.locator("#crumb")).to_have_text(label)
    for width, height in ((320, 568), (390, 844), (768, 1024), (1280, 720)):
        page.set_viewport_size({"width": width, "height": height})
        assert link.evaluate("""link => {
            const box = link.getBoundingClientRect();
            const range = document.createRange();
            range.selectNodeContents(link);
            return box.left >= 0 && box.right <= innerWidth &&
                [...range.getClientRects()].every(r => r.left >= box.left && r.right <= box.right &&
                    r.top >= box.top && r.bottom <= box.bottom);
        }"""), f"Backup menu label clipped at {width}x{height}"
    if touch:
        page.set_viewport_size({"width": 390, "height": 844})
    form = page.locator("#backupForm")
    targets = form.get_by_role("textbox", name="스위치 IP", exact=True)
    expect(targets).to_be_visible()
    expect(form.get_by_text("SSH 포트", exact=False)).to_have_count(0)
    expect(page.locator("#backupSSHPort")).to_have_count(0)
    error = form.locator("#backupTargetError")
    submit = form.get_by_role("button", name="백업 시작")
    targets.fill(switch.ip + "\n" + endpoint + "\n999.1.1.1")
    submit.click()
    expect(error).to_contain_text("2행")
    expect(error).to_contain_text("3행")
    expect(page.get_by_role("dialog", name="SSH 접속 계정")).to_be_hidden()
    assert not jobs and not switch.auth_attempts and not switch.commands

    with page.expect_file_chooser() as chooser:
        form.get_by_role("button", name="대상 열기").click()
    legacy = switch.ip + "\n" + switch.ip + ":2222"
    chooser.value.set_files(
        {"name": "switches.txt", "mimeType": "text/plain", "buffer": legacy.encode()}
    )
    expect(targets).to_have_value(legacy)
    expect(error).to_contain_text("2행")
    targets.fill(switch.ip)
    expect(error).to_be_hidden()
    with page.expect_download() as saved:
        form.get_by_role("button", name="대상 저장").click()
    assert saved.value.path().read_text(encoding="utf-8-sig").splitlines() == [switch.ip]
    if touch:
        submit.tap()
    else:
        submit.click()
    enter_ssh_credentials(page, switch, touch=touch)
    expect(page.locator("#backupResult")).to_contain_text("성공", timeout=90000)
    expect(page.locator('#backupFiles a[download$=".txt"]')).to_be_visible()
    requests = [job["body"] for job in jobs if job["action"] == "backup.run"]
    assert len(requests) == 1
    assert requests[0]["ips"] == switch.ip and not {"targets", "ssh_port"} & requests[0].keys()
    assert switch.commands.count("show running-config") == 1
    assert "configure terminal" not in switch.commands
    if touch:
        assert not errors, errors
        return

    invalid_password = secrets.token_urlsafe()
    http.job("session.open", username=switch.username, password=invalid_password)
    page.locator("#attempts").fill("1")
    folder = folder / "추가 백업"
    page.locator("#backupDirectory").fill(str(folder))
    page.locator("#backupForm .primary").click()
    expect(page.locator("#backupDiagnostics")).to_be_visible(timeout=90000)
    text = page.locator("#backupDiagnostics pre").text_content()
    assert text.startswith("A3F1-") and text.endswith(" \N{MULTIPLICATION SIGN} 1")
    assert switch.ip not in text and switch.username not in text
    expect(page.locator("#backupResult")).to_contain_text("지문 시도")
    expect(page.locator("#backupResult")).to_contain_text("백업 시도")
    expect(page.locator("#backupResult")).to_contain_text("기타 실패 1대")
    (saved_report,) = folder.glob("*/*/result.xlsx")
    expect(page.locator("#backupResult")).to_contain_text(str(saved_report.parent))
    report = page.locator('#backupFiles a[download$=".xlsx"]')
    with page.expect_download() as download:
        report.click()
    assert download.value.path().read_bytes() == saved_report.read_bytes()
    expect(
        page.locator("#backupResult").get_by_role("button", name="결과 폴더 열기")
    ).to_be_visible()

    page.locator("#backupTargets").fill(endpoint + " " + invalid_password)
    page.locator("#uiInspectorOpen").click()
    page.get_by_role("button", name="요소 선택", exact=True).click()
    page.locator("#backupTargets").click()
    expect(page.locator("#uiInspectorDialog")).to_be_visible()
    expect(
        page.locator("#uiInspectorDialog option").filter(has_text="SSH 접속 설정")
    ).to_have_count(0)
    expect(page.locator("#uiInspectorDialog option").filter(has_text="작업 이력")).to_have_count(0)
    text = page.locator("#uiRequestPreview").inner_text()
    assert (
        "UI 식별자: backupTargets" in text
        and invalid_password not in text
        and switch.ip not in text
    )
    expect(
        page.locator("#uiInspectorDialog option").filter(has_text="기본 SSH 포트")
    ).to_have_count(0)
    page.get_by_role("button", name="작업 요청 복사").click()
    assert "backupTargets" in page.evaluate("navigator.clipboard.readText()")
    page.locator("#uiInspectorDialog").get_by_role("button", name="닫기").click()
    page.get_by_role("button", name="식별 종료").click()
    expect(page.locator("#uiInspectorDock")).to_be_hidden()
    assert not errors, errors


def main():
    root = Path(os.environ.get("ARUBA_WEB_ROOT", Path(__file__).resolve().parents[1]))
    python = os.environ.get("ARUBA_WEB_PYTHON", sys.executable)
    engine = sys.argv[1] if len(sys.argv) > 1 else "chromium"
    touch = engine == "webkit"
    with (
        tempfile.TemporaryDirectory() as temp,
        backup_switch() as switch,
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
                browser = launch_test_browser(getattr(playwright, engine))
                try:
                    context = browser.new_context(
                        has_touch=touch,
                        permissions=[] if touch else ["clipboard-read", "clipboard-write"],
                    )
                    exercise(
                        context.new_page(),
                        http,
                        http.base + "/start/" + info["launch"],
                        switch,
                        Path(temp),
                        touch=touch,
                    )
                finally:
                    browser.close()
                http.post("/api/shutdown", {})
                assert child.wait(10) == 0
            finally:
                if child.poll() is None:
                    child.terminate()
    print(
        f"PASS {engine}: IP-only backup target validation, file import/export, and config read over loopback SSH 22."
    )


if __name__ == "__main__":
    main()

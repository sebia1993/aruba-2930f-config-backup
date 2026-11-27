"""Exercise inventory mapping, concurrent role sessions, retry, and exports via Chrome."""

import json
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from urllib.parse import urlsplit

from browser_ping import observe, run_browser_test
from localweb.ping_tools import PingObservation
from openpyxl import load_workbook
from playwright.sync_api import expect

from aruba2930f_backup.models import CollectionFailure, ErrorCode

SW_A, SW_B = "192.0.2.10", "192.0.2.11"
SHARED, NMS, BB = "198.51.100.1", "198.51.100.5", "198.51.100.9"
EXPECTED = Counter(
    [
        (SW_A, "GW", SHARED),
        (SW_A, "BB", SHARED),
        (SW_A, "NMS", NMS),
        (SW_B, "GW", SHARED),
        (SW_B, "BB", BB),
    ]
)
calls = []
cohort = None
release_first_run = Event()


@contextmanager
def simulated_device(_plan, target, credentials, _cancel):
    if credentials.username != "fixture" or credentials.password != "fixture":
        raise CollectionFailure(ErrorCode.AUTH_FAILED, "Fixture switch denied login")
    used = False

    def run(plan, dest, *, cancel, before_send=None):
        nonlocal used
        assert not used, "Expected a fresh SSH connection per role"
        used = True
        calls.append((target.ip, dest.label, dest.ip))
        if target.ip == SW_A and cohort is not None:
            cohort.wait()
        if not release_first_run.wait(20):
            raise AssertionError("The browser did not observe the active job")
        result = observe(plan, target, dest, credentials, cancel, before_send=before_send)
        if (target.ip, dest.label) in {(SW_A, "BB"), (SW_B, "GW")}:
            return replace(
                result, observation=PingObservation("no_reply", plan.options.repetitions, 0, 100)
            )
        return result

    yield SimpleNamespace(run_pair=run)


def exercise(page, url):
    global cohort
    errors = []
    submissions = []

    def record_run_submission(request):
        path = urlsplit(request.url).path
        if request.method == "POST" and path in {"/api/ping/runs", "/api/ping/long/runs"}:
            submissions.append(path)

    page.on("request", record_run_submission)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.set_viewport_size({"width": 1280, "height": 720})
    pending_bootstrap = []
    page.route("**/api/bootstrap", lambda route: pending_bootstrap.append(route), times=1)
    page.goto(url)
    page.evaluate("location.hash = '#ping'")
    expect(page.locator("#pingInputMode")).to_have_value("inventory")
    page.locator("#pingInventoryText").fill(
        "GW IP\t장비 hostanme\t장비 주소\r\n198.51.100.20\tSW-1\t192.0.2.1"
    )
    expect(page.locator("#pingInventoryStatus")).to_contain_text("로컬 서버 연결을 기다리는 중")
    expect(page.locator("#pingStart")).to_be_disabled()
    pending_bootstrap[0].continue_()
    expect(page.locator("#pingInventoryStatus")).to_contain_text("SW IP 열을 선택")
    page.locator('[data-inventory-field="swIp"]').select_option("2")
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 1조합")
    page.locator("#pingPreview").click()
    expect(page.locator("#pingCommands")).to_have_value(
        "ping 198.51.100.20 repetitions 5 timeout 2 source 192.0.2.1"
    )
    many = ["장비 Hostname\tSW IP\tBB"] + [
        f"SW-{i}\t10.0.{i // 250}.{1 + i % 250}\t{BB}" for i in range(301)
    ]
    page.locator("#pingInventoryText").fill("\r\n".join(many))
    expect(page.locator("#pingInventoryStatus")).to_contain_text("선택 0대")
    page.get_by_role("checkbox", name="2행 SW-0 선택", exact=True).check()
    expect(page.locator("#pingInventoryAll")).to_be_disabled()
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 1조합")
    expect(page.locator('#pingInventory input[type="file"]')).to_have_count(0)
    rejected = page.evaluate("""async () => {
      const response = await fetch('/api/ping/inventory/preview', {
        method:'POST', headers:{'Content-Type':'application/json','X-Local-Token':ui.token},
        body:JSON.stringify({text:'SW IP\\tBB\\n192.0.2.1\\t198.51.100.1', filename:'old.xlsx'})
      });
      return response.status;
    }""")
    assert rejected == 400
    page.locator("#pingInventoryText").fill(
        "\r\n".join(
            [
                "BB\tNMS server\t장비 hostanme\tGW IP\tSW IP",
                f"{SHARED}\t{NMS}\tA동\t{SHARED}\t{SW_A}",
                f"{BB}\t\tB동\t{SHARED}\t{SW_B}",
                "\t\t미지정\t\t192.0.2.13",
                f"\t\t제외\t{SHARED}\t192.0.2.14",
                "invalid\t\t입력오류\t\t192.0.2.15",
            ]
        )
    )
    expect(page.locator("#pingInventoryStatus")).to_contain_text("오류 1행")
    expect(page.locator("#pingInventoryRows")).to_contain_text("6행 BB")
    expect(page.get_by_role("checkbox", name="6행 입력오류 선택")).to_be_disabled()
    page.get_by_role("checkbox", name="5행 제외 선택").uncheck()
    page.locator("#pingInventoryMap summary").click()
    page.locator('[data-inventory-field="hostname"]').select_option("")
    expect(page.get_by_role("checkbox", name="3행 " + SW_B + " 선택")).to_be_visible()
    if not page.locator("#pingInventoryMap").evaluate("node => node.open"):
        page.locator("#pingInventoryMap summary").click()
    page.locator('[data-inventory-field="hostname"]').select_option("2")
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 5조합")
    expect(page.get_by_role("checkbox", name="5행 제외 선택")).not_to_be_checked()
    page.locator('[data-inventory-role="nmsIp"]').uncheck()
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 4조합")
    page.locator('[data-inventory-role="nmsIp"]').check()
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 5조합")
    page.locator("#pingOptions > summary").click()
    page.locator("#pingSourceMode").select_option("ip")
    page.locator("#pingSources").fill(f"{SW_A}:22,192.0.2.200\n{SW_B}:22,192.0.2.201")
    page.get_by_role("checkbox", name="3행 B동 선택").uncheck()
    page.locator("#pingPreview").click()
    expect(page.locator("#pingCommands")).to_have_value(
        f"ping {SHARED} repetitions 5 timeout 2 source 192.0.2.200\n"
        f"ping {SHARED} repetitions 5 timeout 2 source 192.0.2.200\n"
        f"ping {NMS} repetitions 5 timeout 2 source 192.0.2.200"
    )
    page.locator("#pingSourceMode").select_option("ssh_ip")
    page.get_by_role("checkbox", name="3행 B동 선택").check()
    page.locator("#pingProfiles summary").click()
    old_plan = page.evaluate("window.readGeneralPingPlan()")
    del old_plan["inventoryRoleSessions"]
    old_plan["options"]["concurrency"] = 1
    profile = {"format": "aruba-local-web-ping", "version": 1, "plan": old_plan}
    page.locator("#pingInputMode").select_option("list")
    page.locator("#pingProfileFile").set_input_files(
        {"name": "old.json", "mimeType": "application/json", "buffer": json.dumps(profile).encode()}
    )
    expect(page.locator("#pingInputMode")).to_have_value("inventory")
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 5조합")
    assert page.evaluate("window.readGeneralPingPlan().options.concurrency") == 3
    page.locator("#pingProfileName").fill("엑셀 입력")
    page.locator("#pingProfileSave").click()
    expect(page.locator("#pingProfileStatus")).to_contain_text("저장했습니다")
    page.locator("#pingInputMode").select_option("list")
    page.locator("#pingProfileLoad").click()
    expect(page.locator("#pingInputMode")).to_have_value("inventory")
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 5조합")
    page.locator("#pingProfiles summary").click()
    page.locator("#pingAdvanced summary").click()
    page.locator("#pingConcurrency").fill("1")
    expect(page.locator("#pingAdvancedSummary")).to_contain_text("동시 SSH 한도 3개")
    page.locator("#pingAdvanced summary").click()
    page.locator("#pingOptions > summary").click()
    page.locator("#pingPreview").click()
    expect(page.locator("#pingPreviewSummary")).to_contain_text("5조합 · 총 25회")
    expect(page.locator("#pingPreviewDest option")).to_have_count(3)
    assert page.locator("#pingPreviewDest").input_value() == "gwIp"
    page.locator("#pingPreviewTarget").select_option(SW_B + ":22")
    expect(page.locator("#pingPreviewDest option")).to_have_count(2)
    page.locator("#pingPreviewDest").select_option("bbIp")
    page.locator("#pingShowPair").click()
    expect(page.locator("#pingCommands")).to_have_value(
        f"ping {BB} repetitions 5 timeout 2 source {SW_B}"
    )
    # Paste a fresh standard table after the optional preview. Start must use the
    # newly parsed input without another read, mapping confirmation, or preview.
    page.locator("#pingInventoryText").fill(
        "\n".join(
            [
                "장비 Hostname\tSW IP\tGW IP\tBB\tNMS server",
                f"A동\t{SW_A}\t{SHARED}\t{SHARED}\t{NMS}",
                f"B동\t{SW_B}\t{SHARED}\t{BB}\t",
                "미지정\t192.0.2.13\t\t\t",
            ]
        )
    )
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 5조합")
    expect(page.locator("#pingInventoryReview")).to_have_js_property("open", False)
    assert not calls and not submissions
    cohort = Barrier(3, timeout=10)
    page.locator("#pingStart").click()
    dialog = page.locator("#queryCredentials")
    expect(dialog).to_be_visible()
    page.locator("#cancelQueryCredentials").click()
    expect(dialog).to_be_hidden()
    expect(page.locator("#pingPreviewStatus")).to_contain_text("취소")
    # Wait for an actual discovery poll to prove cancellation survives refresh.
    with page.expect_response(
        lambda response: (
            response.request.method == "GET" and urlsplit(response.url).path == "/api/ping/runs"
        )
    ) as discovery:
        pass
    assert discovery.value.json()["id"] is None and not submissions and not calls
    expect(page.locator("#pingPreviewStatus")).to_contain_text("취소")
    page.locator("#pingStart").click()
    expect(dialog).to_be_visible()
    page.locator("#queryUsername").fill("fixture")
    page.locator("#queryPassword").fill("fixture")
    try:
        page.locator("#submitQueryCredentials").click()
        expect(dialog).to_be_hidden()
        expect(page.locator("#pingRunStatus")).to_contain_text("진행", timeout=10000)
        near_start = page.locator("#pingPreviewStatus")
        expect(near_start).to_contain_text("SSH 접속 및 Ping 측정 중", timeout=10000)
        expect(near_start).to_contain_text("0/5")
        expect(near_start).to_be_in_viewport()
        assert submissions == ["/api/ping/runs"]
    finally:
        release_first_run.set()
    expect(page.locator("#pingRunStatus")).to_contain_text("작업 종료", timeout=20000)
    expect(near_start).to_contain_text("작업 종료")
    expect(near_start).to_contain_text("5/5")
    assert Counter(calls) == EXPECTED
    cohort = None
    page.reload()
    expect(page.locator("#pingResultView")).to_have_value("inventory", timeout=10000)
    result = page.locator("#pingRows tbody tr")
    expect(result).to_have_count(3)
    a = result.filter(has_text="A동")
    b = result.filter(has_text="B동")
    expect(a.locator('[data-label="GW 결과"]')).to_contain_text("정상")
    expect(a.locator('[data-label="BB 결과"]')).to_contain_text("응답 없음")
    expect(b.locator('[data-label="GW 결과"]')).to_contain_text("응답 없음")
    expect(b.locator('[data-label="NMS 결과"]')).to_contain_text("대상 없음")
    page.locator("#pingDestinationFilter").select_option(SW_A + ":22/gwIp")
    expect(result).to_have_count(1)
    expect(a.locator('[data-label="GW 결과"]')).to_contain_text("정상")
    expect(a.locator('[data-label="BB 결과"]')).to_contain_text("필터 제외")
    page.locator("#pingDestinationFilter").select_option("")
    with page.expect_download() as download:
        page.locator("#pingExportFiltered").click()
    report = load_workbook(BytesIO(Path(download.value.path()).read_bytes()), read_only=True)
    try:
        values = list(report["장비별 결과"].values)
        assert len(values) == 4 and values[1][0] == "A동"
        assert "정상" in values[1][5] and "응답 없음" in values[1][6]
        assert "응답 없음" in values[2][5] and values[2][7] == "대상 없음"
    finally:
        report.close()
    a.locator('[data-label="GW 결과"] input').check()
    expect(a.locator('[data-label="BB 결과"] input')).not_to_be_checked()
    a.locator('[data-label="BB 결과"] input').check()
    page.locator("#pingRerunSelectedFailed").click()
    expect(page.locator("#pingRunCounts")).to_contain_text("1/1", timeout=20000)
    retry = Counter([(SW_A, "BB", SHARED)])
    assert Counter(calls) == EXPECTED + retry
    expect(a.locator('[data-label="GW 결과"]')).to_contain_text("미선택")
    expect(a.locator('[data-label="BB 결과"]')).to_contain_text("응답 없음")

    page.locator("#pingProfiles summary").click()
    page.locator("#pingProfileList").select_option("엑셀 입력")
    page.locator("#pingProfileLoad").click()
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 5조합")
    page.locator("#pingProfiles summary").click()
    page.evaluate("location.hash = '#connect'")
    page.locator("#logout").click()
    expect(page.locator("#connectionState")).to_have_text("계정 미설정")
    page.evaluate("location.hash = '#ping'")
    page.locator('[data-ping-pane="long"]').click()
    page.locator("#pingLongCount").fill("1")
    expect(page.locator("#pingLongEstimate")).to_contain_text("총 5회")
    expect(page.locator("#pingLongCount")).to_be_focused()
    cohort = Barrier(3, timeout=10)
    page.locator("#pingLongStart").click()
    expect(dialog).to_be_visible()
    assert "/api/ping/long/runs" not in submissions
    page.locator("#queryUsername").fill("fixture")
    page.locator("#queryPassword").fill("fixture")
    page.locator("#submitQueryCredentials").click()
    expect(dialog).to_be_hidden()
    expect(page.locator("#pingLongStatus")).to_contain_text("목표 완료", timeout=25000)
    cohort = None
    assert submissions.count("/api/ping/long/runs") == 1
    page.locator("#pingLongDetail summary").click()
    long_rows = page.locator("#pingLongDevices tbody tr")
    expect(long_rows).to_have_count(3)
    expect(long_rows.filter(has_text="B동")).to_contain_text("SSH 실패 0")
    expect(long_rows.filter(has_text="A동").locator('[data-label="GW 결과"]')).to_contain_text(
        "평균 RTT 1 ms"
    )
    expect(long_rows.filter(has_text="A동").locator('[data-label="BB 결과"]')).to_contain_text(
        "손실 1"
    )
    expect(long_rows.filter(has_text="B동").locator('[data-label="GW 결과"]')).to_contain_text(
        "손실 1"
    )
    assert Counter(calls) == EXPECTED + EXPECTED + retry
    page.set_viewport_size({"width": 390, "height": 840})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2")
    # Render a run admitted by a pre-role-session client, whose two role cells share one sample.
    old_plan["inventoryRows"] = [old_plan["inventoryRows"][0]]
    old_plan["inventoryRoles"] = ["gwIp", "bbIp"]
    page.evaluate(
        """async plan => {
      const id = 'e'.repeat(32);
      await api('/api/ping/runs', {requestId:id, plan});
      sessionStorage.setItem('ping.run.id.v1', id);
    }""",
        old_plan,
    )
    page.reload()
    expect(page.locator("#pingRunCounts")).to_contain_text("1/1", timeout=10000)
    legacy = page.locator("#pingRows tbody tr")
    legacy.locator('[data-label="GW 결과"] input').check()
    expect(legacy.locator('[data-label="BB 결과"] input')).to_be_checked()
    assert calls[-1] == (SW_A, "GW / BB", SHARED)
    # A locally registered account can still be denied by the switch. Check
    # that the operator can correct it from the persistent message beside Start.
    page.locator("#pingNew").click()
    page.evaluate("location.hash = '#connect'")
    page.locator("#username").fill("fixture-denied")
    page.locator("#password").fill("fixture-denied")
    page.locator("#loginForm button.primary").click()
    expect(page.locator("#connectionState")).to_contain_text("fixture-denied")
    page.evaluate("location.hash = '#ping'")
    page.locator("#pingInventoryText").fill(f"SW IP\tGW IP\n{SW_A}\t{SHARED}")
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 1조합")
    page.locator("#pingStart").click()
    expect(page.locator("#pingPreviewStatus")).to_contain_text("AUTH_FAILED", timeout=15000)
    expect(page.locator("#pingAccountSettings")).to_be_visible()
    page.locator("#pingRefresh").click()
    expect(page.locator("#pingPreviewStatus")).to_contain_text("비밀번호")
    page.locator("#pingAccountSettings").click()
    expect(page.locator("#loginForm")).to_be_visible()
    assert not errors, errors


if __name__ == "__main__":
    run_browser_test(exercise, simulated_device, account_set=False)
    print(
        "PASS automatic inventory input, SSH account prompt, visible run progress, results, export and rerun."
    )

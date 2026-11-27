"""Exercise inventory mapping, both schedulers, retry, and role-shaped exports via Chrome."""

from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from browser_ping import observe, run_browser_test
from localweb.ping_tools import PingObservation
from openpyxl import load_workbook
from playwright.sync_api import expect

SW_A, SW_B = "192.0.2.10", "192.0.2.11"
SHARED, NMS, BB = "198.51.100.1", "198.51.100.5", "198.51.100.9"
EXPECTED = Counter([(SW_A, SHARED), (SW_A, NMS), (SW_B, SHARED), (SW_B, BB)])
calls = []


def simulated_pair(plan, target, dest, credentials, cancel, *, before_send=None):
    result = observe(plan, target, dest, credentials, cancel, before_send=before_send)
    calls.append((target.ip, dest.ip))
    if (target.ip, dest.ip) == (SW_B, SHARED):
        return replace(
            result, observation=PingObservation("no_reply", plan.options.repetitions, 0, 100)
        )
    return result


@contextmanager
def simulated_device(_plan, target, credentials, _cancel):
    def run(plan, dest, *, cancel, before_send=None):
        return simulated_pair(plan, target, dest, credentials, cancel, before_send=before_send)

    yield SimpleNamespace(run_pair=run)


def exercise(page, url):
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(url)
    page.evaluate("location.hash = '#ping'")
    page.locator("#pingInputMode").select_option("inventory")
    page.locator("#pingInventoryText").fill(
        "GW IP\t장비 hostanme\t장비 주소\r\n198.51.100.20\tSW-1\t192.0.2.1"
    )
    page.locator("#pingInventoryRead").click()
    expect(page.locator("#pingInventoryStatus")).to_contain_text("SW IP 열을 선택")
    page.locator('[data-inventory-field="swIp"]').select_option("2")
    page.locator("#pingInventoryApply").click()
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 1조합")
    page.locator("#pingPreview").click()
    expect(page.locator("#pingCommands")).to_have_value(
        "ping 198.51.100.20 repetitions 5 timeout 2 source 192.0.2.1"
    )
    many = ["장비 Hostname\tSW IP\tBB"] + [
        f"SW-{i}\t10.0.{i // 250}.{1 + i % 250}\t{BB}" for i in range(301)
    ]
    page.locator("#pingInventoryText").fill("\r\n".join(many))
    page.locator("#pingInventoryRead").click()
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
    page.locator("#pingInventoryRead").click()
    expect(page.locator("#pingInventoryStatus")).to_contain_text("오류 1행")
    expect(page.locator("#pingInventoryRows")).to_contain_text("6행 BB")
    expect(page.get_by_role("checkbox", name="6행 입력오류 선택")).to_be_disabled()
    page.get_by_role("checkbox", name="5행 제외 선택").uncheck()
    page.locator("#pingInventoryMap summary").click()
    page.locator('[data-inventory-field="hostname"]').select_option("")
    page.locator('[data-inventory-field="hostname"]').select_option("2")
    page.locator("#pingInventoryApply").click()
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 4조합")
    expect(page.get_by_role("checkbox", name="5행 제외 선택")).not_to_be_checked()
    page.locator('[data-inventory-role="nmsIp"]').uncheck()
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 3조합")
    page.locator('[data-inventory-role="nmsIp"]').check()
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 4조합")
    page.locator("#pingSourceMode").select_option("ip")
    page.locator("#pingSources").fill(f"{SW_A}:22,192.0.2.200\n{SW_B}:22,192.0.2.201")
    page.get_by_role("checkbox", name="3행 B동 선택").uncheck()
    page.locator("#pingPreview").click()
    expect(page.locator("#pingCommands")).to_have_value(
        f"ping {SHARED} repetitions 5 timeout 2 source 192.0.2.200\n"
        f"ping {NMS} repetitions 5 timeout 2 source 192.0.2.200"
    )
    page.locator("#pingSourceMode").select_option("ssh_ip")
    page.get_by_role("checkbox", name="3행 B동 선택").check()
    page.locator("#pingProfiles summary").click()
    page.locator("#pingProfileName").fill("엑셀 입력")
    page.locator("#pingProfileSave").click()
    expect(page.locator("#pingProfileStatus")).to_contain_text("저장했습니다")
    page.locator("#pingInputMode").select_option("list")
    page.locator("#pingProfileLoad").click()
    expect(page.locator("#pingInputMode")).to_have_value("inventory")
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 4조합")
    page.locator("#pingProfiles summary").click()
    page.locator("#pingPreview").click()
    expect(page.locator("#pingPreviewSummary")).to_contain_text("4조합 · 총 20회")
    page.locator("#pingPreviewTarget").select_option(SW_B + ":22")
    expect(page.locator("#pingPreviewDest option")).to_have_count(2)
    page.locator("#pingPreviewDest").select_option(BB)
    page.locator("#pingShowPair").click()
    expect(page.locator("#pingCommands")).to_have_value(
        f"ping {BB} repetitions 5 timeout 2 source {SW_B}"
    )
    assert not calls, calls
    page.locator("#pingStart").click()
    expect(page.locator("#pingRunStatus")).to_contain_text("작업 종료", timeout=20000)
    assert Counter(calls) == EXPECTED
    page.reload()
    expect(page.locator("#pingResultView")).to_have_value("inventory", timeout=10000)
    result = page.locator("#pingRows tbody tr")
    expect(result).to_have_count(3)
    a = result.filter(has_text="A동")
    b = result.filter(has_text="B동")
    expect(a.locator('[data-label="GW 결과"]')).to_contain_text("정상")
    expect(a.locator('[data-label="BB 결과"]')).to_contain_text("5/5")
    expect(b.locator('[data-label="GW 결과"]')).to_contain_text("응답 없음")
    expect(b.locator('[data-label="NMS 결과"]')).to_contain_text("대상 없음")
    with page.expect_download() as download:
        page.locator("#pingExportFiltered").click()
    report = load_workbook(BytesIO(Path(download.value.path()).read_bytes()), read_only=True)
    try:
        values = list(report["장비별 결과"].values)
        assert len(values) == 4 and values[1][0] == "A동" and values[1][5] == values[1][6]
        assert "응답 없음" in values[2][5] and values[2][7] == "대상 없음"
    finally:
        report.close()
    a.locator('[data-label="GW 결과"] input').check()
    expect(a.locator('[data-label="BB 결과"] input')).to_be_checked()
    b.locator('[data-label="GW 결과"] input').check()
    page.locator("#pingRerunSelectedFailed").click()
    expect(page.locator("#pingRunCounts")).to_contain_text("1/1", timeout=20000)
    assert Counter(calls) == EXPECTED + Counter([(SW_B, SHARED)])

    page.locator("#pingProfiles summary").click()
    page.locator("#pingProfileList").select_option("엑셀 입력")
    page.locator("#pingProfileLoad").click()
    expect(page.locator("#pingInventoryStatus")).to_contain_text("측정 4조합")
    page.locator("#pingProfiles summary").click()
    page.locator('[data-ping-pane="long"]').click()
    page.locator("#pingLongCount").fill("1")
    page.locator("#pingLongPreview").click()
    expect(page.locator("#pingLongEstimate")).to_contain_text("총 4회")
    page.locator("#pingLongStart").click()
    expect(page.locator("#pingLongStatus")).to_contain_text("목표 완료", timeout=25000)
    page.locator("#pingLongDetail summary").click()
    long_rows = page.locator("#pingLongDevices tbody tr")
    expect(long_rows).to_have_count(3)
    expect(long_rows.filter(has_text="B동")).to_contain_text("SSH 실패 0")
    expect(long_rows.filter(has_text="A동").locator('[data-label="BB 결과"]')).to_contain_text(
        "평균 RTT 1 ms"
    )
    expect(long_rows.filter(has_text="B동").locator('[data-label="GW 결과"]')).to_contain_text(
        "손실 1"
    )
    assert Counter(calls) == EXPECTED + EXPECTED + Counter([(SW_B, SHARED)])
    page.set_viewport_size({"width": 390, "height": 840})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2")
    assert not errors, errors


if __name__ == "__main__":
    run_browser_test(exercise, simulated_device, simulated_pair)
    print(
        "PASS inventory paste, mapping, profile, preview, per-device results, Excel export, rerun and long Ping."
    )

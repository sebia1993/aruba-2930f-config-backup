"""Check stage progress from a real loopback SSH query and cancellable job."""

from browser_power_matrix import PowerMatrixSwitch
from browser_qa import enter_ssh_credentials, run_browser_case
from playwright.sync_api import expect


def exercise(page, url, switch, second, shots, _):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    page.locator('nav a[href="#ports"]').click()
    meter = page.get_by_role("progressbar", name="현재 작업 단계 진행률")
    expect(meter).to_be_hidden()
    page.locator("#queryTargets").fill(f"127.0.0.1:{switch.port} 1-3\n127.0.0.1:{second.port} 1")

    initial = []

    def inspect_submission(route):
        if route.request.post_data_json["action"] == "ports.query":
            initial.append(
                page.locator("#jobProgress").evaluate(
                    "p => !p.hidden && p.matches(':indeterminate')"
                )
            )
        route.continue_()

    page.route("**/api/jobs", inspect_submission)
    switch.allow_lldp.clear()
    try:
        page.locator("#queryPorts").click()
        enter_ssh_credentials(page, switch)
        expect(meter).to_have_attribute("max", "4", timeout=30000)
        expect(meter).to_have_attribute("value", "1", timeout=30000)
        expect(meter).to_have_attribute("aria-valuetext", "1 / 4")
        expect(page.locator("#jobPercent")).to_have_text("25%")
        expect(page.locator("#statusText")).to_contain_text("1 / 4")
        if shots:
            page.screenshot(path=shots / "phone-job-progress.png")
        expect(page.locator("#statusText")).to_contain_text("PoE 수집 4/4", timeout=25000)
        assert meter.evaluate("p => p.style.getPropertyValue('--power-read')") == "100%"
        assert initial == [True]
        page.locator("#stopJob").click()
        expect(page.locator("#statusText")).to_contain_text("취소를 요청했습니다")
        expect(page.locator("#jobPercent")).to_be_hidden()
        assert meter.evaluate("p => p.matches(':indeterminate')")
        page.emulate_media(reduced_motion="reduce")
        assert meter.evaluate("p => getComputedStyle(p).animationName") == "none"
    finally:
        switch.allow_lldp.set()
    expect(page.locator("#stopJob")).to_be_hidden(timeout=60000)
    expect(meter).to_be_hidden()
    page.locator("#queryPorts").click()
    expect(page.locator("#statusText")).to_have_text("포트 조회 완료", timeout=60000)
    expect(meter).to_be_hidden()
    page.set_viewport_size({"width": 1366, "height": 768})
    page.locator("#shutdown").click()
    page.locator("#reviewBody button").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    run_browser_case(exercise, switch_factory=PowerMatrixSwitch)
    print(
        "PASS job progress: indeterminate start, actual completed/total, cancel, reduced motion and completion"
    )

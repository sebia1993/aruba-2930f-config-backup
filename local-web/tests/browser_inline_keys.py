"""Exercise first-use in-job SSH approval in an isolated Chrome/loopback session."""

import browser_qa
from playwright.sync_api import expect


def inline_keys(page, url, first, second, shots, output):
    page.goto(url)
    addr = f"127.0.0.1:{first.port}"
    other = f"127.0.0.1:{second.port}"
    page.locator('nav a[href="#connect"]').click()
    page.locator("#username").fill(first.username)
    page.locator("#password").fill(first.password)
    page.locator("#loginForm button.primary").click()
    expect(page.locator("#connectionState")).to_contain_text(first.username)

    page.locator('nav a[href="#backup"]').click()
    page.locator("#backupTargets").fill(addr + "\n" + other)
    page.locator("#backupForm .primary").click()
    review = page.locator("#hostReview")
    expect(review.locator("label")).to_have_count(2, timeout=90000)
    assert not first.auth_attempts and not second.auth_attempts
    review.locator("label").filter(has_text=addr).locator("input").check()
    review.get_by_role("button", name="선택한 지문 승인").click()
    expect(page.locator("#backupResult")).to_contain_text("성공", timeout=90000)
    assert first.auth_attempts and not second.auth_attempts
    page.locator('nav a[href="#home"]').click()
    expect(page.locator("#homeBackup")).to_contain_text("구성 백업")

    page.locator('nav a[href="#ports"]').click()
    page.locator("#portLiveTab").click()
    page.locator("#queryTargets").fill(other + " 1")
    page.locator("#queryForm button.primary").click()
    expect(review).to_contain_text(other, timeout=90000)
    review.get_by_role("button", name="새 지문 모두 건너뛰기").click()
    expect(page.locator("#statusText")).to_contain_text("조회 완료", timeout=90000)
    assert not second.auth_attempts
    page.locator("#portLiveTab").click()
    page.locator("#queryForm button.primary").click()
    expect(review.locator("input")).to_be_visible(timeout=90000)
    review.locator("input").check()
    review.get_by_role("button", name="선택한 지문 승인").click()
    expect(page.locator("#portTable")).to_contain_text("8.1 W", timeout=90000)
    page.reload()
    expect(page.locator("#portTable")).to_contain_text("8.1 W", timeout=30000)
    assert str(second.port) in page.locator("#queryTargets").input_value()
    page.locator('nav a[href="#connect"]').click()
    page.locator("#listKeys").click()
    page.get_by_role("button", name="여러 저장 지문 제거").click()
    choices = page.locator("#reviewBody input")
    expect(choices).to_have_count(2)
    for checkbox in choices.all():
        checkbox.check()
    page.get_by_role("button", name="선택한 등록 지문 제거").click()
    expect(page.locator("#knownKeys")).to_contain_text("없습니다.")
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다")


if __name__ == "__main__":
    browser_qa.exercise = inline_keys
    browser_qa.main()
    print("PASS in-job key approval, skip before auth, resume, home, and workspace restore")

"""Verify first-use SSH key registration and changed-key blocking with two loopback switches."""

import browser_qa
from playwright.sync_api import expect
from verify_http_ssh import backup_switch


def first_use_keys(page, url, first, second, shots, output):
    page.goto(url)
    addr = f"{first.ip}:{first.port}"
    other = f"{second.ip}:{second.port}"

    page.locator('nav a[href="#backup"]').click()
    page.locator("#backupTargets").fill(first.ip + "\n" + second.ip)
    page.locator("#backupForm .primary").click()
    browser_qa.enter_ssh_credentials(page, first)
    expect(page.locator("#backupResult")).to_contain_text("성공", timeout=90000)
    assert first.auth_attempts and second.auth_attempts
    expect(page.locator('nav a[href="#home"],[data-page="home"]')).to_have_count(0)
    page.evaluate("location.hash = 'home'")
    page.wait_for_url("**/#ports")
    expect(page.locator('nav a[href="#ports"]')).to_have_attribute("aria-current", "page")
    page.locator("#queryTargets").fill(other + " 1")
    page.locator("#queryForm button.primary").click()
    observed = page.frame_locator("#matrixViewer").locator("#tbody")
    expect(observed).to_contain_text("8.1 W", timeout=90000)
    page.reload()
    expect(observed).to_contain_text("8.1 W", timeout=30000)
    assert second.ip in page.locator("#queryTargets").input_value()
    # A different key on the same endpoint must not replace the original pin.
    saved_fingerprint = second.host_key.fingerprint
    auth_count = len(second.auth_attempts)
    command_count = len(second.commands)
    second.host_key = first.host_key
    page.locator('nav a[href="#backup"]').click()
    page.locator("#backupTargets").fill(second.ip)
    page.locator("#backupForm .primary").click()
    expect(page.locator("#backupResult")).to_contain_text("HOST_KEY_CHANGED", timeout=90000)
    assert len(second.auth_attempts) == auth_count and len(second.commands) == command_count
    page.evaluate("location.hash = 'connect'")
    page.locator("#listKeys").click()
    for endpoint, fingerprint in ((addr, first.host_key.fingerprint), (other, saved_fingerprint)):
        entry = page.locator("#knownKeys tbody tr").filter(has_text=endpoint)
        expect(entry).to_contain_text(fingerprint)
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
    browser_qa.run_browser_case(
        first_use_keys, switch_factory=backup_switch, second_switch_factory=backup_switch
    )
    print(
        "PASS first-use key registration, workspace restore, changed-key blocking and stored pins"
    )

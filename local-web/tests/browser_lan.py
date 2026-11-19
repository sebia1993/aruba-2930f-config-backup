"""Exercise HTTPS sharing in Chrome against an isolated application using an actual local NIC."""

import hashlib
import socket
import ssl
from urllib.parse import urlsplit

from browser_qa import enter_ssh_credentials, run_browser_case
from playwright.sync_api import Error, expect


def open_invitation(page, link):
    # Exception text from browser navigation normally includes the entire one-use URL.
    try:
        response = page.goto(link, wait_until="domcontentloaded")
    except Error:
        raise AssertionError("Chrome could not reach the LAN listener") from None
    return response.status


def check_fingerprint(host, port, fingerprint):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection((host, port), timeout=8) as sock,
        context.wrap_socket(sock, server_hostname=host) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    assert hashlib.sha256(der).digest().hex(":").upper() == fingerprint


def exercise(page, url, switch, *_):
    page.goto(url)
    page.locator("#lanAccess").click()
    expect(page.locator("#lanDialog")).to_be_visible()
    expect(page.locator("#lanAddress option").first).to_be_attached()
    selected = page.locator("#lanAddress").input_value()
    page.locator("#lanStart").click()
    expect(page.locator("#lanRunning")).to_be_visible()
    invitation = page.locator("#lanLink").input_value()
    endpoint = urlsplit(invitation)
    assert endpoint.scheme == "https" and endpoint.hostname == selected and endpoint.port == 18766
    check_fingerprint(selected, endpoint.port, page.locator("#lanFingerprint").text_content())
    base = endpoint.scheme + "://" + endpoint.netloc
    browser = page.context.browser
    with (
        browser.new_context(ignore_https_errors=True) as remote,
        browser.new_context(ignore_https_errors=True) as second,
    ):
        peer = remote.new_page()
        guest = second.new_page()
        assert peer.goto(base + "/api/bootstrap").status == 403
        assert open_invitation(peer, invitation) == 200
        expect(peer.locator("nav")).to_be_visible()
        assert peer.url.rstrip("/") == base
        assert (
            peer.evaluate("fetch('/api/bootstrap').then(r=>r.json()).then(v=>v.lanClient)") is True
        )
        expect(peer.locator("#lanAccess")).to_be_hidden()
        expect(peer.locator("#shutdown")).to_be_hidden()
        assert open_invitation(guest, invitation) == 403
        cookies = remote.cookies(base)
        assert any(c["secure"] and c["httpOnly"] and c["sameSite"] == "Strict" for c in cookies)
        assert (
            peer.evaluate("""async () => {
          const session=await (await fetch('/api/bootstrap')).json();
          const response=await fetch('/api/shutdown', {method:'POST',
              headers:{'Content-Type':'application/json','X-Local-Token':session.token},body:'{}'});
          return response.status;
        }""")
            == 403
        )

        # Query only the disposable loopback SSH fixture created by this test.
        peer.locator('nav a[href="#ports"]').click()
        peer.locator("#queryTargets").fill(f"127.0.0.1:{switch.port} 1")
        peer.locator("#queryPorts").click()
        enter_ssh_credentials(peer, switch)
        matrix = peer.frame_locator("#matrixViewer")
        expect(matrix.locator("#matrix [data-port-key]")).to_have_count(1, timeout=90000)
        expect(matrix.locator("#sourceName")).to_have_text("실시간 조회 결과")

        with page.expect_response(lambda response: response.url.endswith("/api/sharing/renew")):
            page.locator("#lanRenew").click()
        page.wait_for_function(
            "prior=>document.getElementById('lanLink').value!==prior", arg=invitation
        )
        assert open_invitation(guest, page.locator("#lanLink").input_value()) == 200
        expect(guest.locator("nav")).to_be_visible()
        page.locator("#lanStop").click()
        expect(page.locator("#lanSetup")).to_be_visible()
        assert not peer.evaluate("fetch('/api/bootstrap').then(r=>r.ok).catch(()=>false)")
    page.locator("#lanDialog form button").click()
    page.locator("#shutdown").click()
    page.locator("#reviewBody button").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    run_browser_case(exercise)
    print(
        "PASS isolated Chrome LAN sharing: actual adapter, TLS fingerprint, one-use admission, "
        "separate browser sessions, remote live query over loopback SSH, restricted host actions, "
        "renewal and listener shutdown."
    )

"""Exercise desktop-to-phone HTTPS sharing against an isolated server on a local NIC."""

import argparse
import hashlib
import socket
import ssl
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from browser_qa import click_quick_poe, enter_ssh_credentials, expect_power_result, run_browser_case
from browser_unified_controls import hit_point, resize_workspace
from playwright.sync_api import Error, expect


def open_invitation(page, link):
    # Exception text from browser navigation normally includes the entire one-use URL.
    try:
        response = page.goto(link, wait_until="domcontentloaded")
    except Error:
        raise AssertionError("Browser could not reach the LAN listener") from None
    return response.status


def request_invitation(context, invitation):
    try:
        return context.request.get(invitation, max_redirects=0)
    except Error:
        raise AssertionError("Could not inspect invitation response") from None


def admit_second_browser(guest, invitation, base):
    # Inspect the actual 303 before following it. WebKit's cookie inspector on
    # Windows reports SameSite=None even when the server sends SameSite=Strict.
    response = request_invitation(guest.context, invitation)
    assert response.status == 303 and response.headers.get("location") == "/"
    cookies = SimpleCookie(response.headers.get("set-cookie", ""))
    assert len(cookies) == 1
    cookie = next(iter(cookies.values()))
    assert cookie["httponly"] and cookie["secure"]
    assert cookie["samesite"].lower() == "strict" and cookie["path"] == "/"
    assert guest.goto(base).status == 200


def check_fingerprint(host, port, fingerprint):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection((host, port), timeout=8) as sock,
        context.wrap_socket(sock, server_hostname=host) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    assert hashlib.sha256(der).digest().hex(":").upper() == fingerprint


def check_phone_navigation(peer):
    nav = peer.get_by_role("navigation", name="주 메뉴")
    links = nav.get_by_role("link")
    expect(links).to_have_count(3)
    expect(peer.locator('nav a[href="#history"], [data-page="history"]')).to_have_count(0)
    # A locator tap alone would silently scroll horizontally to a clipped menu item.
    for width, height, route in (
        (320, 568, "backup"),
        (360, 800, "logs"),
        (844, 390, "backup"),
        (390, 844, "ports"),
    ):
        resize_workspace(peer, width, height)
        for link in links.all():
            rect = link.bounding_box()
            assert rect["width"] >= 44 and rect["height"] >= 44, (route, rect)
            assert rect["x"] >= 0 and rect["x"] + rect["width"] <= width + 1, (route, rect)
            hit_point(link)
        target = nav.locator(f'a[href="#{route}"]')
        peer.touchscreen.tap(*hit_point(target))
        expect(target).to_have_attribute("aria-current", "page")
        expect(peer.locator(f'[data-page="{route}"]')).to_be_visible()
        assert peer.evaluate(
            "width => innerWidth === width && document.documentElement.scrollWidth <= width", width
        )
    server_info = peer.locator("#mobileServerInfo summary")
    server_info.tap()
    expect(peer.locator("#version")).to_be_visible()
    expect(peer.locator("#lanAccess")).to_be_hidden()
    expect(peer.locator("#shutdown")).to_be_hidden()
    server_info.tap()


def check_phone_port_control(peer, switch, shots):
    address = f"127.0.0.1:{switch.port}"
    switch.off_watts = 0
    peer.locator("#queryTargets").fill(address + " 1")
    peer.locator("#queryPorts").tap()
    enter_ssh_credentials(peer, switch, touch=True)
    matrix = peer.frame_locator("#matrixViewer")
    tile = matrix.locator("#matrix [data-port-key]")
    expect(tile).to_have_count(1, timeout=90000)
    expect(matrix.locator("#sourceName")).to_have_text("실시간 조회 결과")
    expect(peer.locator("#queryTargets")).to_be_hidden()
    expect(peer.locator("#editQueryTargets")).to_be_visible()
    expect(peer.locator("#controlPanel")).not_to_have_attribute("open", "")
    changes = peer.get_by_role("button", name="변화만 보기", exact=True)
    peer.touchscreen.tap(*hit_point(changes))
    expect(tile).to_have_count(0)
    peer.touchscreen.tap(*hit_point(changes))
    expect(tile).to_have_count(1)
    rect = tile.bounding_box()
    assert rect["width"] >= 44 and rect["height"] >= 44, rect
    peer.touchscreen.tap(*hit_point(tile))
    expect(matrix.locator("#detail .observed-watts")).to_have_text("8.1 W")
    assert matrix.locator("html").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    assert matrix.locator(".tablewrap").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    if shots:
        peer.screenshot(path=shots / "lan-phone-ports.png")
    matrix.locator("#inspectObservedPort").tap()
    expect(peer.locator("#selectedCount")).to_have_text("선택 1개")
    expect(peer.locator("#controlPanel")).to_have_attribute("open", "")
    for selector in ("#quickOn", "#quickOff"):
        hit_point(peer.locator(selector))
    with click_quick_poe(peer, "poe_off", [(address, "1")], touch=True):
        expect_power_result(peer, address, "8.1 W", "0 W", delta="\N{MINUS SIGN}8.1 W")
    assert switch.commands.count("no interface 1 power-over-ethernet") == 1
    expect(matrix.locator("#tbody tr[data-port-key]")).to_contain_text("0 W")
    changes.tap()
    expect(tile).to_have_attribute("data-changed", "true")
    tile.tap()
    expect(matrix.locator("#portComparison")).to_contain_text("8.1 W")
    expect(matrix.locator("#portComparison")).to_contain_text("0 W")
    # Keep the observed result and selected route when rotating the phone.
    resize_workspace(peer, 844, 390)
    expect(peer.locator("nav a[aria-current=page]")).to_have_attribute("href", "#ports")
    expect_power_result(peer, address, "8.1 W", "0 W", delta="\N{MINUS SIGN}8.1 W")
    assert peer.evaluate("innerWidth === 844 && document.documentElement.scrollWidth <= 844")
    if shots:
        peer.screenshot(path=shots / "lan-phone-landscape.png")


def exercise(page, url, switch, _second_switch, shots, _backup):
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
        browser.new_context(
            ignore_https_errors=True,
            is_mobile=True,
            has_touch=True,
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            locale="ko-KR",
        ) as remote,
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
        assert request_invitation(second, invitation).status == 403
        cookies = remote.cookies(base)
        assert len(cookies) == 1 and cookies[0]["secure"] and cookies[0]["httpOnly"]
        assert peer.evaluate("document.cookie") == ""
        assert (
            peer.evaluate("""async () => {
          const session=await (await fetch('/api/bootstrap')).json();
          const response=await fetch('/api/shutdown', {method:'POST',
              headers:{'Content-Type':'application/json','X-Local-Token':session.token},body:'{}'});
          return response.status;
        }""")
            == 403
        )

        check_phone_navigation(peer)
        check_phone_port_control(peer, switch, shots)

        with page.expect_response(lambda response: response.url.endswith("/api/sharing/renew")):
            page.locator("#lanRenew").click()
        page.wait_for_function(
            "prior=>document.getElementById('lanLink').value!==prior", arg=invitation
        )
        admit_second_browser(guest, page.locator("#lanLink").input_value(), base)
        expect(guest.locator("nav")).to_be_visible()
        page.locator("#lanStop").click()
        expect(page.locator("#lanSetup")).to_be_visible()
        assert not peer.evaluate("fetch('/api/bootstrap').then(r=>r.ok).catch(()=>false)")
    page.locator("#lanDialog form button").click()
    page.locator("#shutdown").click()
    page.locator("#reviewBody button").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--webkit", action="store_true", help="use installed Playwright WebKit")
    engine = "webkit" if parser.parse_args().webkit else "chromium"
    run_browser_case(exercise, browser_engine=engine)
    print(
        f"PASS isolated {engine} LAN sharing: actual adapter, TLS fingerprint, one-use admission, "
        "separate browser sessions, touch navigation, mobile portrait/landscape, remote SSH query, "
        "single-tap PoE OFF with before/after power verification, restricted host actions, "
        "renewal and listener shutdown."
    )

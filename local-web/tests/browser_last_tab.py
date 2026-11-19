"""Verify automatic server exit using real Chrome windows and a temporary loopback SSH switch."""

import json
import msvcrt
import sqlite3
import time
from contextlib import closing, suppress
from threading import Event
from unittest.mock import patch
from urllib.parse import urlsplit

from browser_qa import enter_ssh_credentials, run_browser_case
from playwright.sync_api import Error, expect
from verify_http_ssh import Http

# Exceed the five-second last-window grace and allow a server polling interval.
AFTER_GRACE = 7


def server_client(url):
    address = urlsplit(url)
    return Http(address.port, address.path.rsplit("/", 1)[1])


def wait_for_exit(directory, timeout=18):
    deadline = time.monotonic() + timeout
    state = directory / "server-info.json"
    while state.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not state.exists(), "Server kept its instance state after the browser closed"
    with (directory / "instance.lock").open("r+b") as lock:
        while True:
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    # The shared harness also waits on the original child process and checks exit code zero.


def tabs_and_windows(page, url, _first, _second, _shots, backup_folder):
    http = server_client(url)
    instance = http.get("/health")["instance"]
    # Launch/health/bootstrap requests from an HTTP client alone do not own the UI.
    time.sleep(AFTER_GRACE)
    assert http.get("/health")["instance"] == instance

    browser = page.context.browser
    context = browser.new_context()
    root = context.new_page()
    root.goto(url)
    expect(root.locator("#version")).to_contain_text("v")
    root.reload()
    expect(root.locator("#version")).to_contain_text("v")

    # Exercise more than Chromium's six HTTP/1 connections to one origin.
    routes = ("/matrix", "/#ports", "/#cable", "/matrix", "/#backup", "/", "/matrix")
    tabs = []
    for path in routes:
        tab = context.new_page()
        tab.goto(http.base + path, timeout=12000)
        tabs.append(tab)
    assert root.evaluate("fetch('/api/bootstrap').then(r => r.ok)")

    matrix_tab = tabs.pop()
    frozen = context.new_cdp_session(matrix_tab)
    frozen.send("Page.setWebLifecycleState", {"state": "frozen"})
    for tab in tabs:
        tab.close()
    root.close()
    # Outlive even the initial 20-second script-attachment window. Only an open viewer keeps it alive.
    time.sleep(22)
    assert http.get("/health")["instance"] == instance, "The frozen port matrix was still open"
    frozen.send("Page.setWebLifecycleState", {"state": "active"})
    expect(matrix_tab.locator("#sourceName")).to_have_text("조회 결과 대기")
    matrix_tab.close()

    # Reopen inside the grace period. No dashboard or iframe is needed for a direct viewer.
    viewer = context.new_page()
    viewer.goto(http.base + "/matrix")
    expect(viewer.locator("#sourceName")).to_have_text("조회 결과 대기")
    time.sleep(AFTER_GRACE)
    assert http.get("/health")["instance"] == instance

    # An independent browser context owns a separate authenticated connection.
    other = browser.new_context()
    last = other.new_page()
    last.goto(url)
    expect(last.locator("#version")).to_contain_text("v")
    context.close()
    time.sleep(AFTER_GRACE)
    assert http.get("/health")["instance"] == instance
    other.close()
    wait_for_exit(backup_folder.parent)


def active_backup(page, url, switch, _second, _shots, backup_folder):
    page.goto(url)
    expect(page.locator("#version")).to_contain_text("v")
    http = server_client(url)
    http.job("session.open", username=switch.username, password=switch.password)
    reading, resume = Event(), Event()
    respond = switch._respond

    def hold_config_read(channel, command):
        if command == "show running-config":
            reading.set()
            assert resume.wait(35), "Test did not release the backup read"
        return respond(channel, command)

    with patch.object(switch, "_respond", hold_config_read):
        try:
            job = http.post(
                "/api/jobs",
                {
                    "action": "backup.run",
                    "body": {
                        "targets": f"127.0.0.1:{switch.port}",
                        "max_attempts": 1,
                        "concurrency": 1,
                    },
                },
            )["id"]
            assert reading.wait(30), "Backup did not reach the loopback switch"
            # Terminate this disposable browser so no unload handler can notify the server.
            browser = page.context.browser
            cdp = browser.new_browser_cdp_session()
            with suppress(Error):
                cdp.send("Browser.crash")
            assert not browser.is_connected(), "Isolated Chrome process did not exit"
            time.sleep(AFTER_GRACE)
            assert http.get(f"/api/jobs/{job}")["state"] == "running"
        finally:
            resume.set()
        directory = backup_folder.parent
        wait_for_exit(directory)

    with closing(sqlite3.connect(directory / "web-history.sqlite")) as db:
        status, files = db.execute("SELECT state, files FROM jobs WHERE id=?", (job,)).fetchone()
        assert status == "done", status
        handles = json.loads(files)
        assert any(entry["name"].endswith(".xlsx") for entry in handles)
    assert any(b"hostname edge-lab" in text.read_bytes() for text in directory.rglob("*.txt"))
    assert switch.commands.count("show running-config") == 1


def monitoring_with_frozen_tab(page, url, switch, _second, _shots, backup_folder):
    page.goto(url)
    page.locator('nav a[href="#ports"]').click()
    page.locator("#queryTargets").fill(f"127.0.0.1:{switch.port} 1")
    page.locator("#monitorInterval").fill("1")
    page.locator("#monitorToggle").click()
    enter_ssh_credentials(page, switch)
    tile = page.frame_locator("#matrixViewer").locator("#matrix [data-port-key]")
    expect(tile).to_be_visible(timeout=90000)
    command = "show lldp info remote-device 1"
    assert command in switch.commands

    # With the browser frozen, only the server can initiate the next scheduled read.
    read_again = Event()
    respond = switch._respond

    def observe_lldp(channel, request):
        result = respond(channel, request)
        if request == command:
            read_again.set()
        return result

    session = page.context.new_cdp_session(page)
    with patch.object(switch, "_respond", observe_lldp):
        session.send("Page.setWebLifecycleState", {"state": "frozen"})
        assert read_again.wait(125), "No scheduled read while the only tab was frozen"
    session.send("Page.setWebLifecycleState", {"state": "active"})
    page.close()
    wait_for_exit(backup_folder.parent, timeout=45)


def early_close(page, url, _first, _second, _shots, backup_folder):
    # Stop before any page script can attach. The authenticated top-level navigation still arms cleanup.
    context = page.context.browser.new_context(java_script_enabled=False)
    tab = context.new_page()
    tab.goto(url)
    expect(tab.locator("#shutdown")).to_have_count(1)
    context.close()
    wait_for_exit(backup_folder.parent, timeout=35)


if __name__ == "__main__":
    for scenario in (tabs_and_windows, active_backup, early_close, monitoring_with_frozen_tab):
        run_browser_case(scenario)
        print(f"PASS last-tab server lifecycle: {scenario.__name__}", flush=True)

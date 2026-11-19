"""Exercise matrix selection, filtered bulk controls and exact SSH endpoints on loopback."""

from threading import Event
from unittest.mock import patch

from browser_qa import (
    click_quick_poe,
    edit_query_targets,
    enter_ssh_credentials,
    expect_power_result,
    open_port_controls,
    run_browser_case,
)
from playwright.sync_api import expect

WORKSPACE_SIZES = ((1366, 768), (1280, 720), (390, 844), (320, 568))


def resize_workspace(page, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate(
        "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"
    )


def hit_point(locator):
    """Check the actual pointer path, including the embedding frame and overlaid controls."""
    point = locator.evaluate("""element => {
        const box = element.getBoundingClientRect();
        let x = box.left + box.width / 2, y = box.top + box.height / 2;
        for (let target = element; target;) {
            const doc = target.ownerDocument;
            const hit = doc.elementFromPoint(x, y);
            if (!hit || !target.contains(hit)) return null;
            target = doc.defaultView.frameElement;
            if (!target) break;
            const frame = target.getBoundingClientRect();
            x += frame.left + target.clientLeft;
            y += frame.top + target.clientTop;
        }
        return [x, y];
    }""")
    assert point is not None, f"{locator} is hidden, covered or outside the viewport"
    return point


def verify_viewport_controls(page, view, endpoint, other_endpoint):
    # Locator.click() and .check() can silently scroll both frame and outer page.
    # Use viewport hit tests and physical clicks to exercise the no-scroll workflow.
    card = page.get_by_role("region", name="포트 상태 매트릭스", exact=True)
    options = card.locator("#controlPanel")
    heading = options.locator("summary")
    search = view.locator("#search")
    tiles = view.locator("#matrix [data-port-key]")
    for width, height in WORKSPACE_SIZES:
        resize_workspace(page, width, height)
        expect(options).not_to_have_attribute("open", "")
        expect(page.locator("#queryTargets")).to_be_hidden()
        for locator in (
            page.locator("#queryPorts"),
            page.locator("#editQueryTargets"),
            *tiles.all(),
        ):
            hit_point(locator)
        selected_tile = view.locator(f'#matrix [data-port-key="{endpoint}/1"]')
        page.mouse.click(*hit_point(selected_tile))
        expect(card.locator("#controlTarget")).to_contain_text(endpoint)
        expect(card.locator("#selectedCount")).to_have_text("선택 1개")
        for selector in ("#quickOff", "#quickOn"):
            button = card.locator(selector)
            expect(button).to_be_enabled()
            hit_point(button)
        expect(view.locator("#tbody input:checked")).to_have_count(0)
        expect(options).not_to_have_attribute("open", "")

        filter_toggle = view.locator("#shortFilterToggle")
        if search.is_hidden():
            page.mouse.click(*hit_point(filter_toggle))
        page.mouse.click(*hit_point(search))
        search.fill(other_endpoint)
        expect(tiles).to_have_count(1)
        expect(card.locator("#quickActions")).to_be_hidden()
        expect(card.locator("#selectedCount")).to_have_text("선택 0개")
        search.fill(endpoint)
        expect(tiles).to_have_count(1)
        expect(card.locator("#quickActions")).to_be_hidden()
        checkbox = view.locator("#tbody input[data-control-key]")
        page.mouse.click(*hit_point(heading))
        expect(options).to_have_attribute("open", "")
        card.locator("#selectVisible").click()
        expect(card.locator("#selectedCount")).to_have_text("선택 1개")
        expect(checkbox).to_be_checked()
        card.locator("#clearSelection").click()
        expect(card.locator("#selectedCount")).to_have_text("선택 0개")
        page.keyboard.press("Escape")
        expect(heading).to_be_focused()
        expect(options).not_to_have_attribute("open", "")
        search.fill("")
        expect(tiles).to_have_count(2)
        if filter_toggle.is_visible():
            filter_toggle.click()
        assert page.evaluate("scrollY === 0 && document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 1440, "height": 960})


def change_target_while_previewing(page, switch, address):
    reading, resume = Event(), Event()
    respond = switch._respond

    def pause_power_read(channel, command):
        if command == "show power-over-ethernet 1":
            reading.set()
            assert resume.wait(30), "Test did not release the port read"
        return respond(channel, command)

    page.locator("#controlMode").select_option("single")
    page.locator("#singleIP").fill(address)
    port = page.locator("#singlePort")
    port.fill("1")
    start = len(switch.commands)
    with patch.object(switch, "_respond", pause_power_read):
        try:
            page.locator("#preview").click()
            assert reading.wait(30), "Single-port preview did not reach PoE read"
            port.fill("2")
        finally:
            resume.set()
        expect(page.locator("#controlResult")).to_contain_text(
            "현재 선택으로 다시 검토", timeout=60000
        )
    expect(page.locator("#review")).to_be_hidden()
    expect(port).to_have_value("2")
    commands = switch.commands[start:]
    assert "show power-over-ethernet 1" in commands and "configure terminal" not in commands
    assert switch.poe_enabled


def select_measured_port(page, matrix_view, changed, available, untouched_switch):
    page.locator("#queryPorts").click()
    changed_row = matrix_view.locator(f'#tbody tr[data-port-key="{changed}/1"] input')
    other_row = matrix_view.locator(f'#tbody tr[data-port-key="{available}/1"] input')
    expect(changed_row).to_be_enabled(timeout=60000)
    matrix_view.locator(f'#matrix [data-port-key="{changed}/1"]').click()
    expect(page.locator("#matrixWorkspace #controlTarget")).to_contain_text(changed)
    expect(changed_row).not_to_be_checked()
    expect(other_row).not_to_be_checked()
    untouched_commands = len(untouched_switch.commands)
    with click_quick_poe(page, "poe_on", [(changed, "1")]):
        result = page.locator("#controlResult tbody tr")
        expect(result).to_have_count(1, timeout=90000)
        expect(result).to_contain_text(changed)
        expect(result).to_contain_text("검증 완료")
    expect_power_result(page, changed, "0 W", "9.7 W", delta="+9.7 W")
    expect(matrix_view.locator(f'#tbody tr[data-port-key="{changed}/1"]')).to_contain_text("9.7 W")
    assert len(untouched_switch.commands) == untouched_commands
    # Two devices share an IP, hostname, interface and AP name. Only the
    # target whose readback changed from measured zero to 9.7 W belongs here.
    changes = page.get_by_role("button", name="변화만 보기", exact=True)
    changes.click()
    changed_tiles = matrix_view.locator("#matrix [data-port-key]")
    expect(changed_tiles).to_have_count(1)
    expect(changed_tiles).to_have_attribute("data-port-key", changed + "/1")
    changed_tiles.click()
    expect(matrix_view.locator("#portComparison")).to_contain_text("0 W")
    expect(matrix_view.locator("#portComparison")).to_contain_text("9.7 W")
    changes.click()
    expect(changed_row).to_be_enabled()
    expect(other_row).to_be_enabled()

    matrix_view.locator(f'#matrix [data-port-key="{changed}/1"]').click()
    mode = page.locator("#controlMode")
    expect(mode).to_have_value("batch")
    expect(changed_row).not_to_be_checked()
    expect(other_row).not_to_be_checked()
    expect(page.locator("#controlTarget")).to_contain_text(changed)


def leave_failed_port_inspection_for_batch(page, matrix_view, failed, available):
    matrix_view.locator(f'#matrix [data-port-key="{failed}/1"]').click()
    matrix_view.locator("#inspectObservedPort").click()
    mode = page.locator("#controlMode")
    expect(mode).to_have_value("single", timeout=60000)
    expect(mode).to_be_disabled()
    expect(page.locator("#singleIP")).to_have_value(failed)
    expect(page.locator("#singlePort")).not_to_be_editable()
    expect(page.locator("#liveScope")).to_be_hidden()

    matrix_view.locator(f'#tbody tr[data-port-key="{available}/1"] input').check()
    expect(mode).to_have_value("batch")
    expect(mode).to_be_enabled()
    expect(page.locator("#liveScope")).to_be_visible()
    expect(matrix_view.locator("#tbody")).to_be_visible()
    expect(page.locator("#controlTarget")).to_contain_text(available)
    mode.select_option("single")
    expect(page.locator("#singleIP")).to_be_editable()
    expect(page.locator("#singlePort")).to_be_editable()


def verify_rejected_draft_and_failed_read(page, view, addresses, first, second):
    targets = edit_query_targets(page)
    submit = page.locator("#queryPorts")
    rows = view.locator("#tbody tr[data-port-key]")
    origin = rows.get_by_role("checkbox")
    origin.check()
    baseline = [len(fixture.commands) for fixture in (first, second)]
    targets.fill("invalid-address 1")
    submit.click()
    expect(page.locator("#statusText")).to_contain_text("인터페이스 목록", timeout=15000)
    expect(targets).to_have_value("invalid-address 1")
    expect(origin).to_be_checked()
    expect(rows).to_have_attribute("data-port-key", addresses[0] + "/1")
    expect(page.locator("#controlTarget")).to_contain_text(addresses[0])
    assert [len(fixture.commands) for fixture in (first, second)] == baseline

    # The other switch is valid but lacks port 2: show its failed observation only.
    targets.fill(addresses[1] + " 2")
    submit.click()
    expect(rows).to_have_attribute("data-port-key", addresses[1] + "/2", timeout=60000)
    expect(rows).to_have_count(1)
    expect(rows.get_by_role("checkbox")).to_be_disabled()
    expect(page.locator("#selectedCount")).to_have_text("선택 0개")
    open_port_controls(page)
    expect(page.locator("#quickOn")).to_be_hidden()
    assert len(first.commands) == baseline[0]
    assert len(second.commands) > baseline[1]
    assert "configure terminal" not in second.commands[baseline[1] :]


def exercise(page, url, switch, second, shots, _backup):
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.goto(url)
    ports_menu = page.get_by_role("link", name="포트·PoE 관리", exact=True)
    ports_menu.click()
    expect(ports_menu).to_have_attribute("href", "#ports")
    expect(page.locator("#crumb")).to_have_text("포트·PoE 관리")
    combined = page.locator("#portMonitorTab")
    expect(combined).to_have_accessible_name("관제")
    workspace = page.locator('[data-page="ports"]')
    expect(workspace.get_by_role("heading", name="포트·PoE 관리", exact=True)).to_have_count(1)
    expect(workspace.locator("#portLiveTab")).to_have_count(0)
    expect(workspace.get_by_role("button", name="실시간 조회", exact=True)).to_have_count(0)
    expect(workspace.locator("#queryForm")).to_be_visible()
    expect(workspace.locator("#queryTargets")).to_be_editable()
    target_options = workspace.locator("#targetOptions > summary")
    target_options.click()
    with page.expect_file_chooser() as picker:
        page.locator("[data-import=queryTargets]").click()
    picker.value.set_files({"name": "targets.csv", "mimeType": "text/csv", "buffer": b"ip,port"})
    expect(page.locator("#statusText")).to_contain_text("허용 형식")
    expect(page.locator("#queryTargets")).to_have_value("")
    page.locator("#dismissStatus").click()
    target_options.click()
    for name in ("관제", "우선 확인", "포트 조회"):
        expect(workspace.get_by_role("button", name=name, exact=True)).to_have_count(1)
    for selector in (
        "#openPortCsv",
        "#portCompareTab",
        "#portTable",
        "#rowDetails",
        "#controlRows",
        "#portSearch",
        "#jumpToControl",
        "#returnToMonitor",
    ):
        expect(workspace.locator(selector)).to_have_count(0)
    expect(page.locator("#portControlTab")).to_have_count(0)
    card = workspace.get_by_role("region", name="포트 상태 매트릭스", exact=True)
    expect(card.locator("#matrixViewer")).to_have_count(1)
    expect(card.locator("#portActionStrip, #matrixFeedback")).to_have_count(2)
    expect(workspace.get_by_text("포트 제어", exact=True)).to_have_count(0)
    panel = card.locator("#controlPanel")
    expect(panel.locator("summary")).to_have_text("제어 옵션")
    expect(page.locator("#singleIP")).to_be_hidden()
    expect(page.locator("#queryPorts")).to_be_enabled()
    subtitle = ports_menu.get_by_text("Port & PoE Management", exact=True)
    # Tablet portrait uses the narrower sidebar with both menu labels visible.
    for width, height in (*WORKSPACE_SIZES, (651, 940), (850, 940)):
        resize_workspace(page, width, height)
        assert ports_menu.evaluate("""link => {
            const menu = link.closest('nav').getBoundingClientRect();
            const box = link.getBoundingClientRect();
            return box.left >= menu.left - 1 && box.right <= menu.right + 1;
        }"""), f"Port menu overflow at {width}px"
        expect(ports_menu).to_have_accessible_name("포트·PoE 관리")
        expect(ports_menu.get_by_text("포트·PoE 관리", exact=True)).to_be_visible()
        expect(ports_menu.locator(".port-nav-icon")).to_be_visible()
        if width >= 651:
            expect(subtitle).to_be_visible()
        for selector in ("#queryTargets", "#queryPorts", "#controlHeading"):
            hit_point(page.locator(selector))
    resize_workspace(page, 1440, 960)
    addresses = [f"127.0.0.1:{fixture.port}" for fixture in (switch, second)]
    matrix_view = page.frame_locator("#matrixViewer")
    expect(matrix_view.locator("#matrix [data-port-key]")).to_have_count(0)
    # Direct target entry remains usable before a matrix query has collected any ports.
    panel.locator("summary").click()
    panel.locator("#singleIP").fill(addresses[1])
    panel.locator("#singlePort").fill("1")
    panel.locator("#preview").click()
    enter_ssh_credentials(page, switch)
    expect(page.locator("#reviewBody tbody tr")).to_contain_text(addresses[1], timeout=60000)
    page.locator("#review").get_by_role("button", name="닫기").click()
    assert "configure terminal" not in second.commands and not switch.commands
    panel.locator("summary").click()
    expect(page.locator("#singleIP")).to_be_hidden()
    page.locator("#queryTargets").fill("\n".join(f"{address} 1" for address in addresses))
    page.get_by_role("button", name="포트 조회", exact=True).click()
    tiles = matrix_view.locator("#matrix [data-port-key]")
    expect(tiles).to_have_count(2, timeout=90000)
    verify_viewport_controls(page, matrix_view, *addresses)
    expect(combined).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#batchControls")).to_be_hidden()
    checkboxes = matrix_view.locator("#tbody input[data-control-key]")
    expect(checkboxes).to_have_count(2)
    expect(matrix_view.locator("#tbody")).to_be_visible()
    expect(page.locator("#matrixGroups")).to_be_hidden()
    for selector in ("#navReview", "#navCompare", "#navOverview", "#openButton", "#addButton"):
        expect(matrix_view.locator(selector)).to_be_hidden()
    expect(page.locator("#queryForm")).to_be_visible()
    expect(page.locator("#matrixViewer")).to_be_visible()
    expect(page.locator("#queryPorts")).to_be_enabled()
    expect(page.locator("#portReviewTab")).to_be_enabled()
    page.locator("#portReviewTab").click()
    expect(matrix_view.locator("#extraView")).to_be_visible()
    expect(page.locator("#queryForm")).to_be_visible()
    page.locator("#portMonitorTab").click()

    # Phone-sized users can explicitly pick and unpick a batch without opening the table.
    resize_workspace(page, 390, 844)
    multi = matrix_view.get_by_role("button", name="여러 포트 선택", exact=True)
    multi.click()
    expect(multi).to_have_attribute("aria-pressed", "true")
    first_tile = matrix_view.locator(f'#matrix [data-port-key="{addresses[0]}/1"]')
    other_tile = matrix_view.locator(f'#matrix [data-port-key="{addresses[1]}/1"]')
    first_tile.click()
    other_tile.click()
    expect(card.locator("#selectedCount")).to_have_text("선택 2개")
    expect(matrix_view.locator("#tbody input:checked")).to_have_count(2)
    first_tile.click()
    multi.click()
    expect(multi).to_have_attribute("aria-pressed", "false")
    expect(card.locator("#controlTarget")).to_contain_text(addresses[1])
    first_tile.click()
    expect(card.locator("#controlTarget")).to_contain_text(addresses[1])
    expect(card.locator("#selectedCount")).to_have_text("선택 1개")
    open_port_controls(page)
    card.locator("#clearSelection").click()
    expect(card.locator("#selectedCount")).to_have_text("선택 0개")
    expect(card.locator("#quickActions")).to_be_hidden()
    page.keyboard.press("Escape")
    resize_workspace(page, 1440, 960)
    assert all("configure terminal" not in fixture.commands for fixture in (switch, second))

    # The device selector distinguishes two switches with the same IP and hostname.
    matrix_view.locator("#device").select_option(addresses[1])
    expect(checkboxes).to_have_count(1)
    open_port_controls(page)
    page.locator("#selectionScope").select_option("switch")
    page.locator("#selectEligible").click()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    expect(checkboxes).to_be_checked()
    draft = edit_query_targets(page)
    saved_targets = draft.input_value()
    baseline = [len(fixture.commands) for fixture in (switch, second)]
    draft.fill(addresses[0] + " 2")
    expect(checkboxes).to_be_checked()
    expect(checkboxes).to_have_count(1)
    expect(matrix_view.locator("#device")).to_have_value(addresses[1])
    expect(matrix_view.locator(f'#matrix [data-port-key="{addresses[1]}/1"]')).to_be_visible()
    expect(page.locator("#controlTarget")).to_contain_text(addresses[1])
    assert [len(fixture.commands) for fixture in (switch, second)] == baseline
    draft.fill(saved_targets)
    page.locator("#clearSelection").click()
    matrix_view.locator("#device").select_option("")
    expect(checkboxes).to_have_count(2)

    change_target_while_previewing(page, switch, addresses[0])

    # The two ports have the same IP, interface, hostname and AP name; only SSH ports differ.
    matrix_view.locator(f'#matrix [data-port-key="{addresses[1]}/1"]').click()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    expect(page.locator("#controlTarget")).to_contain_text(addresses[1])
    expect(
        matrix_view.locator(f'#tbody tr[data-port-key="{addresses[1]}/1"] input')
    ).not_to_be_checked()
    expect(
        matrix_view.locator(f'#tbody tr[data-port-key="{addresses[0]}/1"] input')
    ).not_to_be_checked()
    expect(page.locator("#matrixViewer")).to_be_visible()
    expect(matrix_view.locator("#overviewRoot")).to_be_visible()
    page.locator("#preview").click()
    expect(page.locator("#reviewBody tbody tr")).to_have_count(1, timeout=60000)
    expect(page.locator("#reviewBody tbody tr")).to_contain_text(addresses[1])
    page.locator("#review").get_by_role("button", name="닫기").click()
    assert all("configure terminal" not in fixture.commands for fixture in (switch, second))

    page.locator("#clearSelection").click()
    expect(page.locator("#quickResult")).to_be_hidden()
    matrix_view.locator("#search").fill(addresses[0])
    expect(checkboxes).to_have_count(1)
    page.locator("#selectionScope").select_option("filtered")
    page.locator("#selectVisible").click()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    expect(
        matrix_view.locator(f'#tbody tr[data-port-key="{addresses[0]}/1"] input')
    ).to_be_checked()
    expect(matrix_view.locator("#search")).to_have_value(addresses[0])
    matrix_view.locator("#search").fill("")
    second_checkbox = matrix_view.locator(f'#tbody tr[data-port-key="{addresses[1]}/1"] input')
    expect(second_checkbox).not_to_be_checked()
    # Looking at the other tile must not replace or enlarge an explicit selection.
    matrix_view.locator(f'#matrix [data-port-key="{addresses[1]}/1"]').click()
    expect(page.locator("#controlTarget")).to_contain_text(addresses[0])
    expect(second_checkbox).not_to_be_checked()
    second_checkbox.check()
    expect(page.locator("#selectedCount")).to_have_text("선택 2개")
    expect(page.locator("#quickResult")).to_be_hidden()
    expect(page.locator("#matrixViewer")).to_be_visible()
    if shots:
        page.screenshot(path=shots / "unified-port-control.png")

    assert all(fixture.poe_enabled for fixture in (switch, second))
    assert all("configure terminal" not in fixture.commands for fixture in (switch, second))
    # Deliver the mutation request, then drop just its HTTP acknowledgement.
    delivered = []
    second.off_watts = 0
    power_read = "show power-over-ethernet 1"
    off_command = "no interface 1 power-over-ethernet"
    respond = switch._respond

    def fail_after_verification(channel, command):
        # Keep the command verification successful. Reject the independent PoE query on A.
        if command == power_read and off_command in switch.commands:
            after_write = switch.commands.index(off_command) + 1
            if power_read in switch.commands[after_write:]:
                switch.commands.append(command)
                channel.sendall("\r\n".join([command, "Permission denied", switch.prompt]).encode())
                return False
        return respond(channel, command)

    def drop_submission_reply(route):
        accepted = route.fetch(max_retries=0)
        assert accepted.status == 202
        job = route.request.post_data_json["body"]["requestId"]
        assert accepted.json()["id"] == job
        delivered.append(job)
        route.abort("connectionreset")

    page.route("**/api/jobs", drop_submission_reply, times=1)
    with (
        patch.object(switch, "_respond", fail_after_verification),
        page.expect_request(
            lambda req: (
                req.method == "GET" and delivered and f"/api/jobs/{delivered[0]}?" in req.url
            )
        ),
        click_quick_poe(page, "poe_off", [(addr, "1") for addr in addresses]),
    ):
        results = page.locator("#controlResult tbody tr")
        expect(results).to_have_count(2, timeout=90000)
        assert sorted(results.locator("td:first-child").all_text_contents()) == sorted(addresses)
        assert all("검증 완료" in row for row in results.all_text_contents())
        expect_power_result(page, addresses[0], "8.1 W", "미측정")
        expect_power_result(page, addresses[1], "8.1 W", "0 W", delta="\N{MINUS SIGN}8.1 W")
    assert len(delivered) == 1
    assert all(not fixture.poe_enabled for fixture in (switch, second))
    for fixture in (switch, second):
        assert fixture.commands.count(off_command) == 1
        after_write = fixture.commands.index(off_command) + 1
        assert fixture.commands[after_write:].count(power_read) >= 2
    expect(page.locator("#matrixViewer")).to_be_visible()
    expect(combined).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#selectedCount")).to_have_text("선택 0개")
    failed_row = matrix_view.locator(f'#tbody tr[data-port-key="{addresses[0]}/1"]')
    ready_row = matrix_view.locator(f'#tbody tr[data-port-key="{addresses[1]}/1"]')
    expect(failed_row.get_by_role("checkbox")).to_be_disabled()
    expect(failed_row).not_to_contain_text("8.1 W")
    expect(ready_row.get_by_role("checkbox")).to_be_enabled()
    expect(ready_row).to_contain_text("0 W")
    leave_failed_port_inspection_for_batch(page, matrix_view, addresses[0], addresses[1])

    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#editQueryTargets")).to_be_visible()
    expect(page.locator("#queryPorts")).to_be_enabled()
    expect(page.locator("#matrixViewer")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if shots:
        page.screenshot(path=shots / "unified-port-mobile.png")
    page.set_viewport_size({"width": 1440, "height": 960})
    # Reload keeps the failed endpoint distinguishable from B's measured 0 W.
    page.reload()
    expect(failed_row.get_by_role("checkbox")).to_be_disabled(timeout=30000)
    expect(failed_row).not_to_contain_text("8.1 W")
    expect(ready_row).to_contain_text("0 W")
    key = addresses[0] + "/1"
    matrix_view.locator(f'#matrix [data-port-key="{key}"]').click()
    untouched_commands = len(second.commands)
    open_port_controls(page)
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/jobs")) as req:
        page.locator("#refreshPorts").click()
    assert req.value.post_data_json["body"]["targets"] == addresses[0] + " 1"
    expect(failed_row.get_by_role("checkbox")).to_be_enabled(timeout=60000)
    assert len(second.commands) == untouched_commands

    # The control verification sees 8.1 W, the first independent reading sees 0 W,
    # and the settled reading sees 9.7 W. The update must come from the latter.
    on_command = "interface 1 power-over-ethernet"
    respond_on = second._respond

    def delayed_draw(channel, command):
        if command == power_read and on_command in second.commands:
            prior = second.commands[second.commands.index(on_command) + 1 :].count(power_read)
            if prior:
                watts = 0 if prior == 1 else 9.7
                second.commands.append(command)
                lines = [
                    command,
                    "Status and Configuration Information for port 1",
                    "Power Enable : Yes",
                    "PoE Port Status : Delivering",
                    f"PD Power Draw : {watts} W",
                    second.prompt,
                ]
                channel.sendall("\r\n".join(lines).encode())
                return False
        return respond_on(channel, command)

    with patch.object(second, "_respond", delayed_draw):
        select_measured_port(page, matrix_view, addresses[1], addresses[0], switch)
    assert second.commands.count(on_command) == 1
    assert second.poe_enabled and not switch.poe_enabled

    # Submitting the edited targets replaces the inventory, preserving the other switch.
    untouched = len(second.commands)
    start = len(switch.commands)
    edit_query_targets(page).fill(addresses[0] + " 1")
    expect(tiles).to_have_count(2)
    expect(page.locator("#controlTarget")).to_contain_text(addresses[1])
    expect(
        matrix_view.locator(f'#tbody tr[data-port-key="{addresses[1]}/1"] input')
    ).not_to_be_checked()
    with page.expect_request(
        lambda req: req.method == "POST" and req.url.endswith("/api/jobs")
    ) as query:
        page.locator("#queryPorts").click()
    assert query.value.post_data_json["action"] == "ports.query"
    assert query.value.post_data_json["body"]["targets"] == addresses[0] + " 1"
    expect(tiles).to_have_count(1, timeout=60000)
    expect(tiles).to_have_attribute("data-port-key", addresses[0] + "/1")
    expect(page.locator("#selectedCount")).to_have_text("선택 0개")
    assert len(second.commands) == untouched
    assert "show power-over-ethernet 1" in switch.commands[start:]
    assert "configure terminal" not in switch.commands[start:]
    verify_rejected_draft_and_failed_read(page, matrix_view, addresses, switch, second)
    assert not page_errors, page_errors
    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    run_browser_case(exercise)
    print(
        "PASS unified live matrix, filtered selection, exact endpoint binding and one-click bulk control"
    )

"""Exercise analysis and alarm import in the real offline web UI over loopback HTTP."""

import re
import sqlite3
from contextlib import closing, nullcontext

from browser_qa import run_browser_case
from playwright.sync_api import expect


def exercise(page, url, _switch, _second, _shots, backup):
    run = "a" * 32
    start = "2026-01-01T00:00:00.000+00:00"
    finish = "2026-01-01T00:00:02.000+00:00"
    with closing(sqlite3.connect(backup.parent / "ping-history.sqlite")) as db, db:
        db.execute(
            """INSERT INTO ping_long_runs(id,started_at,finished_at,state,fingerprint,settings,initial_eta)
                   VALUES(?,?,?,'done','browser-fixture','{}',0)""",
            (run, start, finish),
        )
        db.execute(
            """INSERT INTO ping_long_series VALUES(?,0,'192.0.2.11',22,'198.51.100.1','','ssh_ip','192.0.2.11',1)""",
            (run,),
        )
        db.execute(
            """INSERT INTO ping_long_samples(run_id,series_id,attempt,scheduled_at,started_at,received_at,
                   finished_at,status,sent,received) VALUES(?,0,1,?,?,?,?,'no_reply',1,0)""",
            (run, start, start, finish, finish),
        )
    page.goto(url)
    page.locator('nav a[href="#ping"]').click()
    page.locator('[data-ping-pane="analysis"]').click()
    page.locator("#paLoad").click()
    expect(page.locator("#paMetrics")).to_contain_text("확인 손실 1")
    expect(page.locator("#paEvents")).to_contain_text("복구 미확인")
    page.locator('[data-pa-tab="heat"]').click()
    page.locator("#paHeat").click()
    expect(page.locator("#paHeatResult")).to_contain_text("무응답")
    page.locator('[data-pa-tab="nms"]').click()
    expect(page.locator("#paFile")).to_have_attribute("accept", ".csv")
    expect(page.locator("#paSheet")).to_have_count(0)
    uploads = []
    page.on(
        "request",
        lambda request: uploads.append(request.url) if request.url.endswith("/nms-file") else None,
    )
    page.locator("#paFile").set_input_files(
        {
            "name": "alarms.xlsx",
            "mimeType": "application/octet-stream",
            "buffer": b"unread workbook",
        }
    )
    page.locator("#paHeaders").click()
    expect(page.locator("#paStatus")).to_contain_text("CSV")
    assert not uploads
    page.locator("#paFile").set_input_files(
        {
            "name": "alarms.csv",
            "mimeType": "text/csv",
            "buffer": b"device,message,start,close\n192.0.2.11,fixture fault,2026-01-01T00:00:00Z,2026-01-01T00:00:05Z\n",
        }
    )
    page.locator("#paHeaders").click()
    for i, field in enumerate(("device", "message", "opened", "closed")):
        page.locator("#paCol" + field).select_option(str(i))
    page.locator("#paNmsPreview").click()
    expect(page.locator("#paImport")).to_be_enabled()
    expect(page.locator("#paImportResult")).to_contain_text("fixture fault")
    page.locator("#paImport").click()
    expect(page.locator("#paStatus")).to_contain_text("가져온 알람 1건")
    page.locator("#paCompare").click()
    expect(page.locator("#paComparison")).to_contain_text("fixture fault")
    with page.expect_download() as download:
        page.get_by_role("link", name="분석 Excel").click()
    assert download.value.suggested_filename.endswith(".xlsx")
    page.set_viewport_size({"width": 390, "height": 840})
    page.locator('[data-pa-tab="report"]').click()
    expect(page.locator("#paReport")).to_be_in_viewport()
    page.locator("#paDelete").locator("xpath=ancestor::details").locator("summary").click()
    page.locator("#paPolicyLoad").click()
    expect(page.locator("#paDays")).to_have_value(re.compile("[0-9]+"))
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#paDelete").click()
    expect(page.locator("#paStatus")).to_contain_text("삭제했습니다.")
    expect(page.locator("#paRun option")).to_have_count(0)
    page.evaluate("() => api('/api/shutdown', {})")


if __name__ == "__main__":
    run_browser_case(exercise, switch_factory=nullcontext, second_switch_factory=nullcontext)
    print(
        "PASS long Ping history, mapped NMS import, correlated alarm, export, mobile and history deletion."
    )

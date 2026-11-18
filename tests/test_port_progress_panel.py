"""Regression tests for the pinned progress display without a real switch."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from aruba2930f_backup.port_progress_panel import PortProgressPanel
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def test_preflight_query_is_visible_before_poe_mutation(app: QApplication) -> None:
    panel = PortProgressPanel()
    panel.start_query(3, preflight=True, action="PoE ON")
    assert "사전 재조회" in panel.header.text()
    assert panel.progress.maximum() == 3
    panel.update_query(1, 3, "192.0.2.10:22", "1/1/12")
    assert panel.progress.value() == 1
    assert "192.0.2.10" in panel.stage.text()
    assert "1/1/12" in panel.stage.text()
    assert panel.progress.isHidden() is False or panel.progress.isVisibleTo(panel)
    panel.close()


def test_batch_progress_tracks_device_port_and_result(app: QApplication) -> None:
    panel = PortProgressPanel()
    panel.start_batch("PoE ON", 4, selected=20, already_on=16)
    assert "20" in panel.counts.text()
    assert "16" in panel.counts.text()
    panel.mark_stage("192.0.2.10:22", "1/1/12", "preflight")
    assert "192.0.2.10" in panel.stage.text()
    assert "1/1/12" in panel.stage.text()
    panel.mark_stage("192.0.2.10:22", "1/1/12", "apply")
    assert "적용" in panel.stage.text()
    panel.record_result("192.0.2.10:22", "1/1/12", "verified", "설정 확인")
    assert panel.progress.value() == 1
    assert "1/4" in panel.counts.text()
    panel.details_toggle.click()
    assert not panel.details_tree.isHidden()
    assert panel.details_tree.topLevelItemCount() == 1
    assert "확인" in panel.details_tree.topLevelItem(0).text(2)
    panel.close()


def test_failed_batch_result_and_new_job_clear_old_details(app: QApplication) -> None:
    panel = PortProgressPanel()
    panel.start_batch("PoE OFF", 2, selected=2)
    panel.mark_stage("192.0.2.10:22", "1", "preflight")
    panel.record_result("192.0.2.10:22", "1", "unverified", "결과 확인 불가")
    assert "확인 불가" in panel.counts.text() or "확인 불가" in panel.stage.text()
    panel.finish_batch("일괄 작업 보류", failed=True)
    assert "보류" in panel.header.text() or "보류" in panel.stage.text()
    panel.start_batch("PoE ON", 1, selected=1)
    assert panel.progress.value() == 0
    assert panel.details_tree.topLevelItemCount() == 0
    panel.close()

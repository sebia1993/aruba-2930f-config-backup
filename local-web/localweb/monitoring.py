"""State of one read-only monitoring plan shared by this local server's browsers."""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aruba2930f_backup.models import ErrorCode


@dataclass
class PortMonitoring:
    id: str
    groups: tuple
    targets: str
    minutes: int
    job: str | None = None
    last_job: str | None = None
    last_finished: str | None = None
    last_success: str | None = None
    error: str = ""
    suspended: bool = False
    slot: int = 0
    skipped_rounds: int = 0
    port_count: int | None = field(init=False)
    due: float = field(init=False)
    next_at: str | None = None

    def __post_init__(self):
        self.due = time.monotonic() + self.minutes * 60
        self.port_count = (
            None
            if any(g.all_ports for g in self.groups)
            else sum(len(g.interfaces) for g in self.groups)
        )

    def advance_to_next_slot(self, *, skipped=False):
        current = time.monotonic()
        period = self.minutes * 60
        if self.due <= current:
            # Advance from the original cadence even if a long read or sleep missed several slots.
            elapsed_slots = int((current - self.due) // period) + 1
            self.slot += elapsed_slots
            if skipped:
                self.skipped_rounds += elapsed_slots
            self.due += elapsed_slots * period
        self.next_at = (datetime.now(UTC) + timedelta(seconds=self.due - current)).isoformat(
            timespec="milliseconds"
        )

    def finished(self, key, result, error, finished, failure_code):
        self.job = None
        self.last_job = key
        self.last_finished = finished
        self.error = error
        rows = (result or {}).get("rows", [])
        discovery_errors = (result or {}).get("discoveryErrors", [])
        if result is not None and "groups" in result:
            groups = result["groups"]
            unresolved = any(g.get("all_ports") and not g["interfaces"] for g in groups)
            self.port_count = None if unresolved else sum(len(g["interfaces"]) for g in groups)
        elif result is None and any(g.all_ports for g in self.groups):
            self.port_count = None
        rejected = list(
            dict.fromkeys(
                f"{row['ip']}:{row['ssh_port']}"
                for row in [*rows, *discovery_errors]
                if row.get("error_code") == ErrorCode.AUTH_FAILED
            )
        )
        if rejected or failure_code == ErrorCode.AUTH_FAILED:
            device = ", ".join(rejected[:3])
            if len(rejected) > 3:
                device += f" 외 {len(rejected) - 3}대"
            self.error = (
                f"{device + ' · ' if device else ''}SSH 인증 거부로 모니터링을 일시 중단했습니다. "
                "모니터링을 중지하고 SSH 계정을 확인한 뒤 다시 시작하세요."
            )
            self.suspended = True
            self.next_at = None
            return
        if not error:
            failed = sum(
                row.get("status") not in {"ok", "unsupported"}
                or (row.get("status") == "ok" and row.get("watts") is None)
                for row in rows
            )
            if discovery_errors:
                self.error = f"스위치 {len(discovery_errors)}대의 포트 목록을 확인하지 못했습니다."
            elif not rows:
                self.error = "이번 조회 결과를 받지 못했습니다."
            elif failed:
                self.error = f"{failed}개 포트의 전력 조회가 지연됐습니다."
            else:
                self.last_success = finished
        self.advance_to_next_slot(skipped=True)

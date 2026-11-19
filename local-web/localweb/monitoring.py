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
    due: float = field(init=False)
    next_at: str | None = None

    def __post_init__(self):
        self.due = time.monotonic() + self.minutes * 60

    def advance_to_next_slot(self):
        current = time.monotonic()
        period = self.minutes * 60
        if self.due <= current:
            # Advance from the original cadence even if a long read or sleep missed several slots.
            self.due += ((current - self.due) // period + 1) * period
        self.next_at = (datetime.now(UTC) + timedelta(seconds=self.due - current)).isoformat(
            timespec="milliseconds"
        )

    def finished(self, key, result, error, finished, failure_code):
        self.job = None
        self.last_job = key
        self.last_finished = finished
        self.error = error
        rows = (result or {}).get("rows", [])
        rejected = list(
            dict.fromkeys(
                f"{row['ip']}:{row['ssh_port']}"
                for row in rows
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
            if not rows:
                self.error = "이번 조회 결과를 받지 못했습니다."
            elif failed:
                self.error = f"{failed}개 포트의 전력 조회가 지연됐습니다."
            else:
                self.last_success = finished
        self.advance_to_next_slot()

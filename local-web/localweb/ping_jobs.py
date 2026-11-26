"""In-memory, finite general Ping batches. Mutate only under the Jobs lock."""

import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from aruba2930f_backup.models import Credentials, DeviceTarget, ErrorCode

from .ping_ssh import PingPairResult
from .ping_tools import Destination, PingObservation, PingPlan

_AUTH_ERRORS = frozenset({ErrorCode.AUTH_FAILED, ErrorCode.ENABLE_FAILED})


def _utc():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class PingSlot:
    index: int
    target: DeviceTarget
    destination: Destination
    started: str
    monotonic_start: float


class PingBatch:
    """One non-replayable command slot at a time, shared with other Jobs work.

    Slot time is a local observation window, not an ICMP packet timestamp.
    Connection/command concurrency is deliberately one in this initial
    adapter. Long-running sampling and durable measurement rows are separate.
    """

    def __init__(self, key: str, plan: PingPlan, credentials: Credentials):
        self.key = key
        self.plan = plan
        self.credentials = credentials
        self.in_flight: PingSlot | None = None
        self.rows: list[dict] = []
        self.stop_reason = ""
        self.stop_state = ""

    @property
    def terminal(self):
        return self.in_flight is None and (
            bool(self.stop_reason) or len(self.rows) == self.plan.total_pairs
        )

    @property
    def state(self):
        if not self.terminal:
            return "running"
        return self.stop_state or "done"

    def stop(self, reason: str, *, state="cancelled"):
        if not self.terminal and not self.stop_reason:
            self.stop_reason = reason
            self.stop_state = state

    def _pair(self, index):
        device, destination = divmod(index, len(self.plan.destinations))
        return self.plan.targets[device], self.plan.destinations[destination]

    def claim(self):
        if self.terminal or self.stop_reason or self.in_flight is not None:
            return None
        index = len(self.rows)
        target, destination = self._pair(index)
        self.in_flight = PingSlot(index, target, destination, _utc(), time.monotonic())
        return self.in_flight

    def _row(self, index, observation, reason, *, may_have_started=False):
        target, destination = self._pair(index)
        return {
            "index": index,
            "ip": target.ip,
            "ssh_port": target.port,
            "destination_ip": destination.ip,
            "destination_label": destination.label,
            "source_mode": self.plan.options.source_mode,
            "requested_source": self.plan.requested_source(target),
            "verified_source_ip": None,
            "command_may_have_started": may_have_started,
            "reason_code": reason,
            **asdict(observation),
            "slot_started_at": None,
            "slot_finished_at": None,
            "duration_ms": None,
        }

    def record(
        self,
        slot: PingSlot,
        result: PingPairResult | None = None,
        *,
        error: ErrorCode | None = None,
        uncertain=False,
    ):
        if slot is not self.in_flight:
            raise ValueError("Ping slot is no longer current.")
        if (result is None) == (error is None):
            raise ValueError("A slot requires exactly one result or error.")
        if result is not None:
            reason = result.transport_error
            row = self._row(
                slot.index,
                result.observation,
                reason,
                may_have_started=result.command_may_have_started,
            )
            row.update(
                verified_source_ip=result.verified_source_ip,
                hostname=result.identity.hostname,
                model=result.identity.model,
                software_version=result.identity.software_version,
            )
        else:
            reason = error
            if uncertain:
                status = "unknown"
            elif error is ErrorCode.CANCELLED:
                status = "not_run"
            elif error in {ErrorCode.MODEL_UNSUPPORTED, ErrorCode.COMMAND_REJECTED}:
                status = "cli_error"
            else:
                status = "ssh_failure"
            row = self._row(
                slot.index,
                PingObservation(status),
                reason,
                may_have_started=uncertain,
            )
        row.update(
            slot_started_at=slot.started,
            slot_finished_at=_utc(),
            duration_ms=max(0, round((time.monotonic() - slot.monotonic_start) * 1000)),
        )
        # Stop opening new sessions on authentication or enable denial.
        if reason in _AUTH_ERRORS:
            self.stop(str(reason), state="failed")
        self.rows.append(row)
        self.in_flight = None

    def snapshot(self, *, after=0, complete=False):
        rows = self.rows
        if self.terminal and self.stop_reason:
            rows = rows + [
                self._row(index, PingObservation("not_run"), self.stop_reason)
                for index in range(len(rows), self.plan.total_pairs)
            ]
        total = self.plan.total_pairs
        page = rows if complete else rows[after : after + 512]
        return {
            "total": total,
            "completed": len(rows),
            "processedPairs": len(self.rows),
            "possiblyStartedPairs": sum(row["command_may_have_started"] for row in self.rows),
            "effectiveConcurrency": 1,
            "stopReason": self.stop_reason,
            "slotActive": self.in_flight is not None,
            "rows": [dict(row) for row in page],
            "cursor": len(rows) if complete else after + len(page),
        }

"""In-memory, finite general Ping batches. Mutate only under the Jobs lock."""

import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from aruba2930f_backup.models import Credentials, DeviceTarget, ErrorCode

from .ping_ssh import PingPairResult
from .ping_tools import (
    MAX_PAIRS,
    Destination,
    PingObservation,
    PingPlan,
    preview_command,
    role_session_waves,
)

_AUTH_ERRORS = frozenset({ErrorCode.AUTH_FAILED, ErrorCode.ENABLE_FAILED})
_FAILED_STATUSES = frozenset(
    {"partial_loss", "no_reply", "unreachable", "source_error", "cli_error", "ssh_failure"}
)


def rerun_indices(request, parent_indices, rows):
    """Map parent result positions back into the original, immutable device/destination matrix."""
    mode = request.get("mode")
    if mode == "selected":
        if set(request) != {"parentId", "mode", "indices"}:
            raise ValueError("선택 재실행 요청을 확인하세요.")
        indices = request["indices"]
        if (
            not isinstance(indices, list)
            or not 1 <= len(indices) <= len(parent_indices) <= MAX_PAIRS
            or any(type(i) is not int or not 0 <= i < len(parent_indices) for i in indices)
            or len(set(indices)) != len(indices)
        ):
            raise ValueError("재실행할 조합 선택을 확인하세요.")
        return tuple(sorted(parent_indices[rows[i].get("pairIndex", i)] for i in indices))
    if set(request) != {"parentId", "mode"}:
        raise ValueError("재실행 요청을 확인하세요.")
    if mode == "all":
        return parent_indices
    if mode != "failed":
        raise ValueError("재실행 종류를 확인하세요.")
    indices = tuple(
        sorted(
            parent_indices[row.get("pairIndex", row["index"])]
            for row in rows
            if row["status"] in _FAILED_STATUSES
        )
    )
    if not indices:
        raise ValueError("재실행할 실패 조합이 없습니다.")
    return indices


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
    """Bounded device workers share the general operation slot with the existing Jobs queue."""

    def __init__(
        self, key: str, plan: PingPlan, credentials: Credentials | None, indices, parent_id=None
    ):
        self.key = key
        self.plan = plan
        self.credentials = credentials
        self.indices = indices
        self.parent_id = parent_id
        self.in_flight: dict[int, PingSlot] = {}
        self.workers: dict[int, DeviceTarget] = {}
        self.remaining: dict[DeviceTarget, deque[int]] = {}
        active_destinations = {}
        active_roles = {}
        for index in range(len(indices)):
            target, destination = self._pair(index)
            self.remaining.setdefault(target, deque()).append(index)
            if plan.inventory:
                active_destinations.setdefault(target.ip, {})[destination.ip] = None
                if plan.inventory_role_sessions:
                    active_roles.setdefault(target.ip, []).extend(
                        plan.inventory.pair_roles[target, destination]
                    )
        self.inventory_rows = [
            {**row, "activeDestinations": list(active_destinations.get(row["swIp"], ()))}
            for row in plan.inventory_rows
        ]
        counts = {target: len(pending) for target, pending in self.remaining.items()}
        self.device_capacity, self.capacity = plan.capacities(counts)
        if plan.inventory_role_sessions:
            for row in self.inventory_rows:
                row["activeRoles"] = active_roles.get(row["swIp"], [])
            if plan.options.concurrent_devices is None:
                self.role_waves = deque(
                    role_session_waves(counts.items(), plan.options.concurrency)
                )
        self.rows: list[dict] = []
        self.unpersisted_after: int | None = None
        self.stop_reason = ""
        self.stop_state = ""

    @property
    def terminal(self):
        return not self.workers and (bool(self.stop_reason) or len(self.rows) == len(self.indices))

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
        return self.plan.pairs[self.indices[index]]

    def next_devices(self):
        if self.plan.inventory_role_sessions and self.plan.options.concurrent_devices is None:
            return self.role_waves.popleft()
        devices = []
        addresses = {target.ip for target in self.workers.values()}
        for target, pending in self.remaining.items():
            if len(addresses) >= self.device_capacity:
                break
            if target.ip not in addresses:
                devices.extend(
                    [target] * (len(pending) if self.plan.inventory_role_sessions else 1)
                )
                addresses.add(target.ip)
        return devices

    def claim(self, target):
        pending = self.remaining.get(target)
        if self.stop_reason or not pending:
            return None
        index = pending.popleft()
        if not pending:
            del self.remaining[target]
        target, destination = self._pair(index)
        slot = PingSlot(index, target, destination, _utc(), time.monotonic())
        self.in_flight[index] = slot
        return slot

    def _row(self, index, observation, reason, *, may_have_started=False):
        target, destination = self._pair(index)
        row = {
            "index": len(self.rows),
            "pairIndex": index,
            "ip": target.ip,
            "ssh_port": target.port,
            "destination_ip": destination.ip,
            "destination_label": destination.label,
            "source_mode": self.plan.options.source_mode,
            "requested_source": self.plan.requested_source(target),
            "planned_command": preview_command(self.plan, target, destination),
            "verified_source_ip": None,
            "command_may_have_started": may_have_started,
            "reason_code": reason,
            **asdict(observation),
            "slot_started_at": None,
            "slot_finished_at": None,
            "duration_ms": None,
        }
        if inventory := self.plan.inventory:
            row["inventory_hostname"] = inventory.by_ip[target.ip].hostname
            row["inventory_roles"] = list(inventory.pair_roles[target, destination])
        return row

    def record(
        self,
        slot: PingSlot,
        result: PingPairResult | None = None,
        *,
        error: ErrorCode | None = None,
        uncertain=False,
    ):
        if slot is not self.in_flight.get(slot.index):
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
            elif error in {ErrorCode.CANCELLED, ErrorCode.REPORT_WRITE_FAILED}:
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
        del self.in_flight[slot.index]

    def snapshot(self, *, after=0, complete=False, durable_only=False):
        rows = self.rows
        if durable_only or (self.unpersisted_after is not None and not complete):
            rows = rows[: self.unpersisted_after]
        processed = len(rows)
        possibly_started = sum(row["command_may_have_started"] for row in rows)
        if self.terminal and self.stop_reason and not durable_only:
            completed = {row["pairIndex"] for row in rows}
            rows = list(rows)
            for index in range(len(self.indices)):
                if index not in completed:
                    row = self._row(index, PingObservation("not_run"), self.stop_reason)
                    row["index"] = len(rows)
                    rows.append(row)
        total = len(self.indices)
        after = min(max(after, 0), len(rows))
        page = rows if complete else rows[after : after + 512]
        snapshot = {
            "parentRunId": self.parent_id,
            "options": self.plan.options.to_dict(),
            "total": total,
            "completed": len(rows),
            "processedPairs": processed,
            "possiblyStartedPairs": possibly_started,
            "effectiveConcurrency": self.capacity,
            "effectiveDevices": self.device_capacity,
            "activeDevices": len({target.ip for target in self.workers.values()}),
            "activeSessions": len(self.workers),
            "stopReason": self.stop_reason,
            "slotActive": bool(self.in_flight),
            "rows": [dict(row) for row in page],
            "cursor": len(rows) if complete else after + len(page),
        }
        if self.plan.inventory:
            snapshot.update(
                inventoryRows=self.inventory_rows,
                inventoryRoles=list(self.plan.inventory_roles),
                inventoryRoleSessions=self.plan.inventory_role_sessions,
            )
        return snapshot

    def record_interrupted(self, pair_index, started):
        row = self._row(
            pair_index,
            PingObservation("unknown"),
            "SERVER_RESTART_DURING_COMMAND",
            may_have_started=True,
        )
        row["slot_started_at"] = started
        self.rows.append(row)

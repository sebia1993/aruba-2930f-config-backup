"""Immutable, confirmed targets and operator-facing outcomes for the manual queue."""

from dataclasses import dataclass

from aruba2930f_backup.port_control import validate_interface

from .controls import _targets
from .operations import _groups


# Fail closed if a progress producer reports endlessly advancing cursors.
# Normal progress pages contain up to 512 events, so 128 pages permit a
# substantially larger history than a single diagnostic job should need.
_MAX_CABLE_PROGRESS_PAGES = 128


@dataclass(frozen=True)
class ManualRequest:
    action: str
    operation: str
    targets: tuple

    @classmethod
    def parse(cls, action, body):
        if action == "control.quick-execute":
            operation = body.get("operation")
            if operation not in ("poe_on", "poe_off"):
                raise ValueError("빠른 제어는 PoE ON 또는 OFF를 선택하세요.")
            targets = tuple(_targets(body))
        else:
            if body.get("confirmed") is not True:
                raise ValueError("선택 포트의 링크 단절 가능성을 확인한 뒤 테스트를 실행하세요.")
            groups = _groups(body, allow_all_ports=False)
            targets = tuple(
                (g.target, validate_interface(port)) for g in groups for port in g.interfaces
            )
            operation = "cable.test"
        return cls(action, operation, targets)

    @property
    def signature(self):
        return self.operation, frozenset(self.targets)

    def public_targets(self):
        return [{"ip": d.ip, "sshPort": d.port, "port": p} for d, p in self.targets]

    def body(self, key):
        if self.action == "control.quick-execute":
            return {"requestId": key, "operation": self.operation, "targets": self.public_targets()}
        grouped = {}
        for target, port in self.targets:
            grouped.setdefault(target, []).append(port)
        return {
            "requestId": key,
            "confirmed": True,
            "targets": "\n".join(
                f"{d.ip}:{d.port} {','.join(ports)}" for d, ports in grouped.items()
            ),
        }


def _row_address(row):
    return (
        row.get("ip"),
        row.get("sshPort", row.get("ssh_port", 22)),
        row.get("port", row.get("interface")),
    )


def _interrupted_tests(rows, progress):
    # Cancelled siblings share stage="test" even when only one command was attempted.
    candidates = {
        _row_address(row)
        for row in rows
        if row.get("status") == "cancelled" and row.get("stage") in {"test", "show"}
    }
    if not candidates:
        return set()
    attempted = set()
    cursor = 0
    for _ in range(_MAX_CABLE_PROGRESS_PAGES):
        page = progress(cursor)
        # A broken progress producer must never trap the queue worker or make
        # a possibly executed cable command look safe to follow with PoE.
        if not isinstance(page, dict) or not isinstance(page.get("cableRows"), list):
            return candidates
        if not page["cableRows"]:
            return candidates & attempted
        for event in page["cableRows"]:
            if not isinstance(event, dict):
                return candidates
            if event.get("stage") in {"test", "show"} and event.get("status") == "pending":
                attempted.add(_row_address(event))
        next_cursor = page.get("cursor")
        if type(next_cursor) is not int or next_cursor <= cursor:
            return candidates
        cursor = next_cursor
    # Truncated progress cannot prove that an individual cable test was not
    # sent. Keep every cancelled test/show port uncertain and block follow-up
    # control rather than holding the worker forever.
    return candidates


def manual_outcome(request, state, result, error, uncertain_failure, *, cable_progress=None):
    """Return display state and the exact ports whose following actions must stop."""
    all_ports = set(request.targets)
    if result is None:
        if uncertain_failure:
            return (
                "uncertain",
                "작업 실행 결과를 기록하거나 확인하지 못했습니다. 포트를 다시 조회하세요.",
                all_ports,
            )
        return state, error or "작업이 취소되었습니다.", set()
    rows = result.get("results", result.get("rows", []))
    by_address = {(d.ip, d.port, p): (d, p) for d, p in request.targets}
    statuses = set()
    interrupted = (
        _interrupted_tests(rows, cable_progress) if request.action == "cable.test" else set()
    )
    uncertain = set()
    for row in rows:
        status = row.get("status")
        statuses.add(status)
        key = _row_address(row)
        if (
            status in {"unverified", "result_unknown", "uncertain", "partial"}
            or key in interrupted
            or (row.get("stage") == "show" and status in {"unrecognized", "failed"})
        ):
            if key in by_address:
                uncertain.add(by_address[key])
            else:
                uncertain.update(all_ports)
    for pair in result.get("powerRefresh", {}).get("readings", []):
        if pair.get("status") != "measured":
            key = (pair.get("ip"), pair.get("sshPort", 22), pair.get("port"))
            if key in by_address:
                uncertain.add(by_address[key])
    if uncertain:
        return (
            "uncertain",
            "결과를 확인하지 못한 포트가 있습니다. 해당 포트를 다시 조회하세요.",
            uncertain,
        )
    if (
        state == "failed"
        or result.get("blocked")
        or statuses & {"failed", "preflight_failed", "unsupported", "skipped", "unrecognized"}
    ):
        return "failed", "작업을 완료하지 못했습니다. 포트별 결과를 확인하세요.", set()
    if state == "cancelled" or statuses & {"cancelled", "paused"}:
        return "cancelled", "작업이 취소되었거나 일부 포트가 실행되지 않았습니다.", set()
    return "done", "작업을 완료했습니다.", set()

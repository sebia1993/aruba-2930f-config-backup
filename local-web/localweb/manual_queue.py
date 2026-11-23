"""Immutable, confirmed targets and operator-facing outcomes for the manual queue."""

from dataclasses import dataclass

from aruba2930f_backup.port_control import validate_interface

from .controls import _targets
from .operations import _groups


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


def manual_outcome(request, state, result, error, uncertain_failure):
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
    uncertain = set()
    for row in rows:
        status = row.get("status")
        statuses.add(status)
        if status in {"unverified", "result_unknown", "uncertain", "partial"}:
            key = (
                row.get("ip"),
                row.get("sshPort", row.get("ssh_port", 22)),
                row.get("port", row.get("interface")),
            )
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

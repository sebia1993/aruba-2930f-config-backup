"""Browser approvals for the existing switch port-control service."""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from aruba2930f_backup.batch_port_control import (
    BATCH_CHECKPOINT,
    MAX_BATCH_ACTIONS,
    BatchControlError,
    BatchPlan,
    BatchPortController,
    BatchStatus,
    create_batch_plan,
    journal_results,
    validate_batch_target,
)
from aruba2930f_backup.models import Credentials, DeviceTarget
from aruba2930f_backup.poe_power import LLDPStatus
from aruba2930f_backup.poe_query_models import PoEQueryResult, PoETarget, QueryStatus
from aruba2930f_backup.port_control import (
    PortAction,
    PortControlService,
    PortSnapshot,
    validate_interface,
)
from aruba2930f_backup.port_control_ssh import port_change_command
from aruba2930f_backup.storage import default_output_directory

from .operations import first_use_host_key_policy

if TYPE_CHECKING:
    from aruba2930f_backup.batch_port_control import _BatchJournal

    from .operations import Operations


@dataclass(repr=False)
class Approval:
    token: str
    text: str
    action: PortAction
    issued: float
    credentials: Credentials
    query: tuple[PoEQueryResult, ...]
    service: PortControlService | None = None
    snapshot: PortSnapshot | None = None
    batch: BatchPlan | None = None
    quick: bool = False
    journal: _BatchJournal | None = None
    offset: int = 0


def _target_fields(target: DeviceTarget, interface: str) -> dict:
    return {"ip": target.ip, "sshPort": target.port, "port": interface}


def _targets(payload: dict) -> list[tuple[DeviceTarget, str]]:
    items = payload.get("targets")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH_ACTIONS:
        raise ValueError("제어할 포트를 1~300개 선택하세요.")
    targets = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("ip"), str):
            raise ValueError("대상 IPv4 주소와 포트를 확인하세요.")
        address = item["ip"]
        ssh_port = item.get("sshPort", 22)
        if ":" in address:
            address, _, port_text = address.rpartition(":")
            if not port_text.isascii() or not port_text.isdigit() or len(port_text) > 5:
                raise ValueError("올바른 SSH 포트를 입력하세요.")
            if "sshPort" in item and ssh_port != int(port_text):
                raise ValueError("대상의 SSH 포트 값이 일치하지 않습니다.")
            ssh_port = int(port_text)
        if type(ssh_port) is not int:
            raise ValueError("SSH 포트는 정수여야 합니다.")
        interface = validate_interface(item.get("port"))
        targets.append((DeviceTarget(address, ssh_port), interface))
    if len(set(targets)) != len(targets):
        raise ValueError("같은 스위치의 포트가 중복되었습니다.")
    return targets


def _detail(enabled: bool, link: str | None, poe: bool | None, watts: float | None) -> str:
    power = "미지원·미측정" if poe is None else ("ON" if poe else "OFF")
    observed_link = {"up": "Up", "down": "Down"}.get((link or "").strip().casefold(), "미확인")
    parts = ["포트 사용" if enabled else "포트 비활성", f"PoE {power}", f"링크 {observed_link}"]
    if watts is not None:
        parts.append(f"{watts:g} W")
    return " · ".join(parts)


def _snapshot_info(snapshot: PortSnapshot | None) -> dict | None:
    if snapshot is None:
        return None
    return {
        **_target_fields(snapshot.target, snapshot.interface),
        "hostname": snapshot.hostname,
        "model": snapshot.model,
        "enabled": snapshot.enabled,
        "link": snapshot.link_status,
        "poeEnabled": snapshot.poe_enabled,
        "poeStatus": snapshot.poe_status,
        "watts": snapshot.watts,
        "observed": snapshot.observed_at,
        "detail": _detail(
            snapshot.enabled, snapshot.link_status, snapshot.poe_enabled, snapshot.watts
        ),
    }


def _needs_read(row: PoEQueryResult | None, action: PortAction) -> bool:
    if (
        not row
        or not row.complete
        or row.snapshot is None
        or row.status not in (QueryStatus.OK, QueryStatus.UNSUPPORTED)
    ):
        return True
    if action is PortAction.POE_ON:
        return row.snapshot.poe_enabled is True
    return not row.lldp or row.lldp.status is not LLDPStatus.OK


class Controls:
    """Called exclusively by the web host's serialized operations worker."""

    def __init__(
        self,
        operations: Operations,
        *,
        clock: Callable[[], float] = time.monotonic,
        service_factory: Callable[[], PortControlService] | None = None,
    ):
        self.operations = operations
        self.directory = Path(operations.data_dir) / "port-control"
        self.legacy_directory = default_output_directory() / "port-control"
        self._factory = service_factory or (
            lambda: PortControlService(
                host_key_store=operations.host_key_store, audit_directory=self.directory
            )
        )
        self._clock = clock
        self._approval: Approval | None = None

    def invalidate(self) -> None:
        self._approval = None

    def dispatch(self, action: str, body: dict, cancel: Event) -> dict:
        if action == "control.history":
            return {"rows": self._history()}
        if action == "control.import-history":
            content = body.get("text")
            if not isinstance(content, str) or len(content.encode("utf-8")) > 2 * 1024 * 1024:
                raise ValueError("2 MiB 이하의 포트 제어 JSONL 기록을 선택하세요.")
            name = body.get("name")
            name = Path(name).name[:128] if isinstance(name, str) and name else "가져온 이력"
            with tempfile.TemporaryDirectory(dir=self.operations.data_dir) as tmp:
                path = Path(tmp) / "history.jsonl"
                path.write_text(content.removeprefix("\ufeff"), encoding="utf-8")
                try:
                    rows = self._read_history_file(path, None, name)
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    AttributeError,
                    IndexError,
                    KeyError,
                ) as exc:
                    raise ValueError(
                        "포트 제어 작업 기록을 읽을 수 없습니다. 원본 JSONL을 확인하세요."
                    ) from exc
            return {"rows": rows}
        if action in {"control.inspect", "control.quick", "control.preview"}:
            self.invalidate()
            if action == "control.inspect":
                target = _targets(body)
                if not self.operations.credentials or len(target) != 1:
                    raise ValueError("SSH 계정을 적용하고 한 개의 포트를 지정하세요.")
                if cancel.is_set():
                    raise ValueError("작업이 중지되었습니다.")
                state = self._factory().inspect(
                    *target[0],
                    self.operations.credentials,
                    approve_host_key=first_use_host_key_policy(cancel),
                )
                return {"snapshot": _snapshot_info(state)}
            if action == "control.quick":
                return self._quick(body, cancel)
            return self._preview(body, cancel)
        if action == "control.apply":
            return self._apply(body, cancel)
        raise ValueError("지원하지 않는 포트 제어 요청입니다.")

    def _preview(self, body: dict, cancel: Event) -> dict:
        credentials = self.operations.credentials
        if credentials is None:
            raise ValueError("먼저 SSH 접속 정보를 입력하세요.")
        if cancel.is_set():
            raise ValueError("포트 제어 미리보기가 중지되었습니다.")
        try:
            action = PortAction(body.get("operation"))
        except ValueError:
            raise ValueError("지원하지 않는 포트 제어 동작입니다.") from None
        targets = _targets(body)
        scope = body.get("scope", "batch")
        if scope == "single":
            if len(targets) != 1:
                raise ValueError("단일 포트 제어는 한 포트만 선택하세요.")
            target, interface = targets[0]
            service = self._factory()
            snapshot = service.inspect(
                target, interface, credentials, approve_host_key=first_use_host_key_policy(cancel)
            )
            if cancel.is_set():
                raise ValueError("포트 제어 미리보기가 중지되었습니다.")
            if action.is_poe and snapshot.poe_enabled is None:
                raise ValueError("이 포트의 PoE 사용 설정을 확인할 수 없습니다.")
            approval = Approval(
                uuid.uuid4().hex,
                snapshot.confirmation_text,
                action,
                self._clock(),
                credentials,
                self.operations.last_results,
                service=service,
                snapshot=snapshot,
            )
        elif scope == "batch":
            query = self.operations.last_results
            rows = {(row.group.target, row.interface): row for row in query}
            try:
                selected = [rows[key] for key in targets]
            except KeyError:
                raise ValueError(
                    "실시간 SSH 조회에 포함된 포트만 선택하세요. 대상을 다시 조회하세요."
                ) from None
            batch = create_batch_plan(selected, action)
            approval = self._batch_approval(batch, credentials, query)
        else:
            raise ValueError("지원하지 않는 제어 방식입니다.")
        return self._offer(approval)

    def _quick(self, body: dict, cancel: Event) -> dict:
        """Refresh missing read-only evidence, then offer one fixed, explicitly approved plan."""
        credentials = self.operations.credentials
        if not credentials:
            raise ValueError("먼저 SSH 접속 정보를 입력하세요.")
        if cancel.is_set():
            raise ValueError("작업이 중지되었습니다.")
        if body.get("operation") not in (PortAction.POE_OFF.value, PortAction.POE_ON.value):
            raise ValueError("빠른 제어는 PoE ON 또는 OFF를 선택하세요.")
        action = PortAction(body["operation"])
        targets = _targets(body)
        inventory = self.operations._inventory
        registered = {(group.target, port) for group in inventory for port in group.interfaces}
        if not set(targets).issubset(registered):
            raise ValueError("먼저 선택한 포트를 조회 대상으로 등록하세요.")
        rows = {(r.group.target, r.interface): r for r in self.operations.last_results}
        refresh = {key for key in targets if _needs_read(rows.get(key), action)}
        reread = {}
        if refresh:
            groups = []
            for group in inventory:
                ports = tuple(port for port in group.interfaces if (group.target, port) in refresh)
                if ports:
                    groups.append(PoETarget(group.target, ports, group.label))
            reread = self.operations._query(tuple(groups), credentials, cancel, refresh=True)
            rows = {(r.group.target, r.interface): r for r in self.operations.last_results}
        if cancel.is_set():
            raise ValueError("사전 조회가 중지되었습니다.")
        ready, skipped, blocked = [], [], []
        for key in targets:
            row = rows.get(key)
            try:
                if row is None:
                    raise BatchControlError("새 포트 조회 결과 없음")
                validate_batch_target(row, action)
            except BatchControlError as exc:
                reason = str(exc)
                if row and row.message:
                    reason += " · " + row.message
                if row and row.lldp and row.lldp.message:
                    reason += " · LLDP: " + row.lldp.message
                blocked.append({**_target_fields(*key), "detail": reason})
                continue
            if action is PortAction.POE_ON and row.snapshot.poe_enabled:
                skipped.append({**_target_fields(*key), "detail": "이미 PoE ON"})
            else:
                ready.append(row)
        response = {
            "skipped": skipped,
            "blocked": blocked,
            "rechecked": len(refresh),
            "selected": len(targets),
        }
        response.update(reread)
        if not blocked and ready:
            batch = create_batch_plan(ready, action)
            approval = self._batch_approval(batch, credentials, self.operations.last_results)
            approval.quick = True
            approval.text = f"EXECUTE {len(batch.targets)}"
            response.update(self._offer(approval))
        return response

    def _batch_approval(
        self, batch: BatchPlan, credentials: Credentials, query: tuple[PoEQueryResult, ...]
    ) -> Approval:
        count = len(batch.targets)
        return Approval(
            uuid.uuid4().hex,
            f"EXECUTE {count}",
            batch.action,
            self._clock(),
            credentials,
            query,
            batch=batch,
        )

    def _offer(self, approval: Approval) -> dict:
        warnings = []
        later = []
        if approval.action in (PortAction.POE_OFF, PortAction.DISABLE):
            warnings.append("이 포트에 연결된 장비의 전원 또는 통신이 중단될 수 있습니다.")
        if approval.snapshot is not None:
            snapshot = approval.snapshot
            targets = [_snapshot_info(snapshot)]
            remaining = 0
            if approval.action in (PortAction.POE_OFF, PortAction.DISABLE):
                warnings.append(
                    "단일 포트 개별 제어입니다. 호스트와 포트가 의도한 장비인지 확인하세요."
                )
        else:
            assert approval.batch is not None
            chunk = (
                approval.batch.targets
                if approval.quick
                else approval.batch.targets[:BATCH_CHECKPOINT]
            )
            remaining = len(approval.batch.targets) - len(chunk)
            targets = []
            observed = {(r.group.target, r.interface): r.snapshot for r in approval.query}
            for item in chunk:
                current = observed.get((item.target, item.interface))
                detail = _detail(
                    item.expected_enabled,
                    current.link_status if current else "",
                    item.expected_poe_enabled,
                    current.watts if current else None,
                )
                if item.confirmed_ap535:
                    detail += " · LLDP AP-535"
                    if item.ap_name:
                        detail += f" {item.ap_name}"
                targets.append(
                    {
                        **_target_fields(item.target, item.interface),
                        "hostname": item.expected_hostname,
                        "detail": detail,
                    }
                )
            if remaining:
                warnings.append(
                    f"이번 승인 후 남은 {remaining}개는 다음 묶음에서 별도로 승인합니다."
                )
                later = [
                    {
                        **_target_fields(item.target, item.interface),
                        "hostname": item.expected_hostname,
                        "command": port_change_command(item.interface, approval.action.value),
                    }
                    for item in approval.batch.targets[len(chunk) :]
                ]
        for item in targets:
            item["command"] = port_change_command(item["port"], approval.action.value)
        self._approval = approval
        return {
            "planId": approval.token,
            "confirmationText": approval.text,
            "operation": approval.action.value,
            "targets": targets,
            "later": later,
            "remaining": remaining,
            "quick": approval.quick,
            "expiresIn": int(PortControlService.SNAPSHOT_MAX_AGE_SECONDS),
            "warnings": warnings,
        }

    def _apply(self, body: dict, cancel: Event) -> dict:
        approval = self._approval
        if approval is None or body.get("planId") != approval.token:
            raise ValueError("실행할 계획이 없습니다. 포트를 다시 검토하세요.")
        # Consume before even opening a socket. A failed or interrupted HTTP
        # response must never make a possibly applied change replayable.
        self.invalidate()
        if (
            approval.credentials is not self.operations.credentials
            or approval.query is not self.operations.last_results
            or not 0
            <= self._clock() - approval.issued
            < PortControlService.SNAPSHOT_MAX_AGE_SECONDS
        ):
            raise ValueError(
                "접속 정보 또는 조회 결과가 바뀌었거나 승인이 만료되었습니다. 다시 검토하세요."
            )
        if body.get("confirmation") != approval.text:
            raise ValueError("확인 문자열이 일치하지 않습니다. 대상을 다시 검토하세요.")
        if cancel.is_set():
            raise ValueError("실행 전에 사용자가 작업을 중지했습니다.")
        if approval.service is not None:
            assert approval.snapshot is not None
            snapshot = approval.snapshot
            try:
                result = approval.service.apply(
                    snapshot,
                    approval.action,
                    approval.credentials,
                    confirmation=approval.text,
                    cancel_event=cancel,
                )
            finally:
                self._forget_rows({(snapshot.target, snapshot.interface)})
            return {
                "results": [
                    {
                        **_target_fields(snapshot.target, snapshot.interface),
                        "status": result.status,
                        "message": result.message,
                        "before": _snapshot_info(snapshot),
                        "after": _snapshot_info(result.after),
                    }
                ],
                "message": result.message,
            }
        assert approval.batch is not None
        batch = approval.batch
        chunk = batch.targets if approval.quick else batch.targets[:BATCH_CHECKPOINT]
        rest = batch.targets[len(chunk) :]
        job = replace(batch, targets=chunk)
        controller = BatchPortController(
            controller_factory=self._factory, journal_directory=self.directory / "batch"
        )
        journal = approval.journal or controller.begin_journal(batch)
        stages = {}

        def publish(index, item, stage, message=""):
            stages[index] = {
                **_target_fields(item.target, item.interface),
                "stage": stage,
                "message": message,
            }
            self.operations.progress = {
                "message": f"{item.target.endpoint} / {item.interface} · {stage}",
                "total": len(chunk),
                "completed": sum(
                    entry["stage"] not in ("사전 검증", "명령 적용") for entry in stages.values()
                ),
                "ports": list(stages.values()),
            }

        try:
            outcome = controller.execute(
                job,
                approval.credentials,
                cancel_event=cancel,
                journal=journal,
                journal_offset=approval.offset,
                continue_after_chunk=(lambda _done, _total: not cancel.is_set())
                if approval.quick
                else None,
                on_stage=lambda index, item, phase: publish(
                    index, item, "사전 검증" if phase == "preflight" else "명령 적용"
                ),
                on_result=lambda index, result: publish(
                    index, result.target, result.status.value, result.message
                ),
            )
        finally:
            self._forget_rows({(target.target, target.interface) for target in chunk})
        result_rows = [
            {
                **_target_fields(r.target.target, r.target.interface),
                "status": r.status.value,
                "message": r.message,
                "after": _snapshot_info(r.after),
            }
            for r in outcome.results
        ]
        response = {
            "results": result_rows,
            "message": f"{len(result_rows)}개 포트의 실행 결과를 기록했습니다.",
        }
        halted = cancel.is_set() or any(
            r.status in (BatchStatus.UNVERIFIED, BatchStatus.CANCELLED, BatchStatus.PAUSED)
            for r in outcome.results
        )
        uncertain = next(
            (r.target for r in outcome.results if r.status is BatchStatus.UNVERIFIED), None
        )
        pause_message = "다음 묶음 실행 전 중단되어 이 포트에 변경 명령을 전송하지 않았습니다."
        if uncertain:
            location = f"{uncertain.target.endpoint} 포트 {uncertain.interface}"
            response["message"] = f"{location}의 변경 결과를 확인하지 못했습니다."
            pause_message = (
                f"{location}의 변경 결과를 확인하지 못해 이 포트에 변경 명령을 전송하지 않았습니다."
            )
            unsent = len(rest) + sum(
                r.status in (BatchStatus.CANCELLED, BatchStatus.PAUSED) for r in outcome.results
            )
            if unsent:
                response["message"] += f" 뒤의 {unsent}개 포트에는 변경 명령을 전송하지 않았습니다."
        if rest and not halted:
            pending = self._batch_approval(
                replace(batch, targets=rest), approval.credentials, self.operations.last_results
            )
            pending.journal = journal
            pending.offset = approval.offset + len(chunk)
            pending.text = f"EXECUTE {min(len(rest), BATCH_CHECKPOINT)}"
            response["nextPlan"] = self._offer(pending)
        elif rest:
            state = BatchStatus.CANCELLED if cancel.is_set() else BatchStatus.PAUSED
            for index in range(approval.offset + len(chunk), approval.offset + len(batch.targets)):
                journal.append(index, "result", status=state.value, message=pause_message)
            if not uncertain:
                response["message"] += f" 남은 {len(rest)}개 포트의 실행을 중단했습니다."
        return response

    def _forget_rows(self, keys: set[tuple[DeviceTarget, str]]) -> None:
        self.operations.last_results = tuple(
            r for r in self.operations.last_results if (r.group.target, r.interface) not in keys
        )

    def _history(self) -> list[dict]:
        # Only app-owned paths, including the desktop app's original audit
        # location. The browser never supplies a filesystem path.
        files = []
        for directory in {self.directory, self.legacy_directory}:
            for batch, folder in ((False, directory), (True, directory / "batch")):
                for path in folder.glob("*.jsonl"):
                    try:
                        info = path.stat()
                        if path.is_file() and info.st_size <= 2_000_000:
                            files.append((info.st_mtime_ns, path, batch))
                    except OSError:
                        continue
        rows = []
        for _, path, batch in sorted(files, reverse=True)[:60]:
            try:
                entries = self._read_history_file(path, batch, path.name)
                rows.extend(dict(entry, _path=str(path)) for entry in entries)
            except OSError, ValueError, TypeError, AttributeError, IndexError, KeyError:
                continue
        return self.operations.history_folders.register(
            "control", rows, path_field="_path", roots=(self.directory, self.legacy_directory)
        )

    def _read_history_file(self, path: Path, batch: bool | None, name: str) -> list[dict]:
        with path.open(encoding="utf-8") as handle:
            record = json.loads(handle.readline())
            if batch is None:
                batch = record.get("event") == "plan"
            if batch:
                results = journal_results(path)
                if len(results) > MAX_BATCH_ACTIONS:
                    raise ValueError("작업 이력의 포트 수가 범위를 벗어났습니다.")
                return [
                    self._history_row(
                        record["created_at"],
                        item["endpoint"],
                        item["interface"],
                        item.get("hostname", ""),
                        record["action"],
                        results[index][0],
                        name,
                        results[index][1],
                    )
                    for index, item in enumerate(record["items"])
                ]
            for line in handle:
                record = json.loads(line)
            status = record["status"]
            return [
                self._history_row(
                    record["time"],
                    record["target"],
                    record["interface"],
                    record["hostname"],
                    record["action"],
                    "result_unknown" if status == "pending" else status,
                    name,
                    record.get("message", ""),
                )
            ]

    @staticmethod
    def _history_row(time_value, endpoint, interface, host, action, status, file, message) -> dict:
        ip, _, ssh_port = endpoint.rpartition(":")
        return {
            "time": time_value,
            "ip": ip,
            "sshPort": ssh_port,
            "port": interface,
            "hostname": host,
            "operation": action,
            "status": status,
            "source": file,
            "message": message if isinstance(message, str) else "",
        }

"""Local browser adapters for the original Aruba 2930F SSH and reporting services."""

from __future__ import annotations

import re
import tempfile
import threading
from collections import Counter
from contextlib import contextmanager, suppress
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aruba2930f_backup.batch_port_control import BatchControlError, validate_batch_target
from aruba2930f_backup.collector import ArubaCollector
from aruba2930f_backup.diagnostics import diagnostic_code_for_result
from aruba2930f_backup.hostkeys import HostKeyStore
from aruba2930f_backup.models import (
    CollectionFailure,
    CollectionOptions,
    Credentials,
    DeviceResult,
    DeviceStatus,
    DeviceTarget,
    DiagnosticDetail,
    DiagnosticPhase,
    ErrorCode,
    HostKeyCheck,
    HostKeyTrustState,
)
from aruba2930f_backup.poe_query import PoEQueryService
from aruba2930f_backup.poe_query_models import (
    MAX_INTERFACES_PER_TARGET,
    PoEQueryResult,
    PoETarget,
    QueryStatus,
)
from aruba2930f_backup.poe_targets import (
    MAX_PASTE_CHARACTERS,
    MAX_PROFILE_BYTES,
    load_target_profile,
    make_target,
    merge_targets,
    parse_pasted_targets,
    save_target_profile,
)
from aruba2930f_backup.port_control import PortAction, validate_interface
from aruba2930f_backup.reporting import ReportSummary, SanitizedJsonlLogger, write_result_workbook
from aruba2930f_backup.ssh_legacy import session_legacy_ssh
from aruba2930f_backup.storage import (
    FilenameMode,
    backup_storage_permission_warnings,
    create_run_directory,
    default_output_directory,
    device_config_path,
    write_config_atomic,
)
from aruba2930f_backup.validation import validate_device_identity

from .backup_destination import copy_backup_files, selected_directory
from .backup_history import backup_history
from .backup_tools import BackupProgress, HistoryFolders, pick_directory
from .cable import CableSession, diagnose_ports
from .port_tools import group_targets, matrix_observation, preview_targets, target_table

POWER_SETTLE_SECONDS = 3


def _target_text(body: dict, field: str = "targets") -> str:
    text = body.get(field)
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_PASTE_CHARACTERS:
        raise ValueError("대상 목록을 1~100,000자로 입력하세요.")
    return text.strip().removeprefix("\ufeff")


def _endpoint(text: str) -> DeviceTarget:
    ip, separator, port = text.partition(":")
    if separator and not re.fullmatch(r"[0-9]{1,5}", port):
        raise ValueError("SSH 포트는 1~65535 사이의 정수로 입력하세요.")
    return DeviceTarget(ip.strip(), int(port) if separator else 22)


class TargetInputError(ValueError):
    """All error lines are generated here, without echoing input values."""


def _switch_targets(body: dict, *, ip_only: bool = False) -> tuple[DeviceTarget, ...]:
    field = "ips" if ip_only else "targets"
    _target_text(body, field)  # Validate length before parsing the original line numbers.
    default_port = 22 if ip_only else _bounded_integer(body, "ssh_port", 22, 65535)
    invalid = (
        "스위치 IPv4 주소만 입력하세요." if ip_only else "올바른 IPv4 주소와 SSH 포트를 입력하세요."
    )
    identity = "스위치 IP" if ip_only else "스위치 주소 및 SSH 포트"
    targets, errors = {}, []
    for number, raw in enumerate(body[field].removeprefix("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            target = (
                _endpoint(line) if not ip_only and ":" in line else DeviceTarget(line, default_port)
            )
        except ValueError:
            errors.append(f"{number}행 · {invalid}")
            continue
        if target in targets:
            errors.append(f"{number}행 · {targets[target]}행과 {identity}가 중복됩니다.")
        else:
            targets[target] = number
    if errors:
        raise TargetInputError("\n".join(errors))
    if not targets:
        raise TargetInputError("스위치 IPv4 주소를 한 개 이상 입력하세요.")
    return tuple(targets)


def _backup_targets(body: dict) -> tuple[DeviceTarget, ...]:
    ip_only = "ips" in body
    if ip_only and ("targets" in body or "ssh_port" in body):
        raise TargetInputError("백업 IP 목록에는 다른 대상이나 SSH 포트를 함께 지정할 수 없습니다.")
    return _switch_targets(body, ip_only=ip_only)


def _expand_interfaces(text: str) -> str:
    # Ranges are expanded here for discovery only. No range enters the SSH interface.
    interfaces = []
    for part in text.split(","):
        part = part.strip()
        if "-" not in part:
            interfaces.append(validate_interface(part))
        else:
            first, _, last = part.partition("-")
            first = validate_interface(first.strip())
            last = validate_interface(last.strip())
            member, _, start = first.rpartition("/")
            last_member, _, stop = last.rpartition("/")
            width = int(stop) - int(start) + 1
            if (
                last_member and last_member != member
            ) or not 1 <= width <= MAX_INTERFACES_PER_TARGET:
                raise ValueError("포트 범위의 시작·끝 번호와 스택 멤버를 확인하세요.")
            prefix = member + "/" if member else ""
            interfaces.extend(f"{prefix}{port}" for port in range(int(start), int(stop) + 1))
        if len(interfaces) > MAX_INTERFACES_PER_TARGET:
            raise ValueError("스위치당 인터페이스는 최대 384개입니다.")
    return ",".join(interfaces)


def _groups(body: dict) -> tuple[PoETarget, ...]:
    text = _target_text(body)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if "\t" in text:
        # Try the original quoted Excel TSV grammar first. A simple two-column
        # table can alternatively use the web's combined endpoint or port range.
        try:
            return parse_pasted_targets(text)
        except ValueError:
            if any(line.count("\t") != 1 for line in lines):
                raise
    if not re.match(r"^[0-9.]+(?::[0-9]+)?\s+", lines[0]):
        return parse_pasted_targets(text)
    groups = []
    for number, line in enumerate(lines, 1):
        try:
            address, ports = line.split(maxsplit=1)
            target = _endpoint(address)
            groups.append(make_target(target.ip, target.port, _expand_interfaces(ports)))
        except ValueError as exc:
            raise ValueError(f"{number}행의 스위치 주소 또는 포트 목록을 확인하세요.") from exc
    return merge_targets(groups)


def _abort_if_cancelled(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise CollectionFailure(ErrorCode.CANCELLED, "작업을 중지했습니다.")


def first_use_host_key_policy(cancel: threading.Event):
    def accept(check: HostKeyCheck) -> bool:
        return check.state is HostKeyTrustState.UNKNOWN and not cancel.is_set()

    return accept


def _bounded_integer(body: dict, name: str, default: int, upper: int) -> int:
    value = body.get(name, default)
    if type(value) is not int or not 1 <= value <= upper:
        raise ValueError(f"{name} 값은 1~{upper}의 정수여야 합니다.")
    return value


def _eligible(row: PoEQueryResult) -> list[str]:
    actions = []
    for action in PortAction:
        try:
            validate_batch_target(row, action)
        except BatchControlError:
            continue
        actions.append(action.value)
    return actions


class Operations:
    """One local operator. The HTTP layer serializes dispatch calls."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.host_key_store = HostKeyStore(self.data_dir / "host_keys.json")
        if self.host_key_store.path.parent != self.data_dir:
            raise ValueError("장비 지문 저장 파일은 데이터 폴더 안에 있어야 합니다.")
        self.legacy_key_store = HostKeyStore()
        self.legacy_backup_dir = default_output_directory()
        self.history_folders = HistoryFolders()
        self.collector = ArubaCollector(host_key_store=self.host_key_store)
        self.query_service = PoEQueryService(host_key_store=self.host_key_store)
        self.credentials: Credentials | None = None
        self.last_results: tuple[PoEQueryResult, ...] = ()
        self._unreported_ports: set[tuple[DeviceTarget, str]] = set()
        self.unverified_controls: set[tuple[DeviceTarget, str]] = set()
        self._inventory: tuple[PoETarget, ...] = ()
        self.query_target_text: str | None = None
        self._review: dict[str, HostKeyCheck] = {}
        self._progress_lock = threading.Lock()
        self.progress: dict = {}
        self._backup_progress = None
        self._query_events = []
        self._query_targets = ()
        self._cable_events = []

    def progress_snapshot(self):
        progress = self.progress
        tracker = self._backup_progress
        if tracker is not None and progress.get("backup_detail"):
            return tracker.snapshot()
        return dict(progress)

    @property
    def query_started(self) -> bool:
        return bool(self._query_targets)

    def query_progress(self, after):
        with self._progress_lock:
            batch = self._query_events[after : after + 512]
            changes = {"observations": batch, "cursor": after + len(batch)}
            if after == 0 and self.query_started:
                changes["targets"] = group_targets(self._query_targets)
            return changes

    def cable_progress(self, after):
        with self._progress_lock:
            rows = self._cable_events[after : after + 512]
            return {"cableRows": rows, "cursor": after + len(rows)}

    def reset_job_progress(self):
        with self._progress_lock:
            self.progress = {}
            self._query_events = []
            self._query_targets = ()
            self._cable_events = []

    def session_summary(self) -> dict:
        credentials = self.credentials
        return {
            "active": credentials is not None,
            "username": credentials.username if credentials else "",
        }

    def inventory_view(self):
        return group_targets(self._inventory)

    def _start_progress(self, message: str, total: int, completed: int = 0) -> None:
        self._backup_progress = None
        self.progress = {"message": message, "total": total, "completed": completed}

    def _progress(
        self, message: str, *, finished: bool = False, cable_row: dict | None = None
    ) -> None:
        # Concurrent native callbacks publish a new immutable-in-use dict for each update.
        with self._progress_lock:
            if cable_row is not None:
                self._cable_events.append(deepcopy(cable_row))
            total = self.progress["total"]
            completed = min(total, self.progress["completed"] + int(finished))
            self._start_progress(message, total, completed)

    def dispatch(self, action: str, body: dict, cancel: threading.Event) -> dict:
        if action != "ports.refresh":
            _abort_if_cancelled(cancel)
        if action in {"targets.preview", "targets.apply"}:
            view = preview_targets(body, _groups)
            if action == "targets.apply":
                self._inventory = _groups({"targets": view["targets"]}) if view["groups"] else ()
                self.last_results = ()
                self.query_target_text = None
                self.unverified_controls.clear()
            return view
        if action == "session.open" or action.startswith("hostkey."):
            self.host_key_store.initialize_from(self.legacy_key_store)
        if action in {"session.open", "session.close"}:
            credentials = None
            if action == "session.open":
                username, password, secret = (
                    body.get("username"),
                    body.get("password"),
                    body.get("enable_secret") or None,
                )
                if (
                    not isinstance(username, str)
                    or not isinstance(password, str)
                    or (secret is not None and not isinstance(secret, str))
                ):
                    raise ValueError("SSH 사용자 이름과 비밀번호를 입력하세요.")
                credentials = Credentials(username.strip(), password, secret)
            self._review.clear()
            self.last_results = ()
            self.unverified_controls.clear()
            self.credentials = credentials
            self.query_target_text = None
            session_legacy_ssh.clear()
            return {"connected": credentials is not None}
        if action == "hostkey.inspect":
            self._review.clear()
            self.collector.begin_run()
            targets = _switch_targets(body)
            self._start_progress("호스트 키 조회 중", len(targets))
            checks = self.collector.probe_host_keys(
                targets, options=CollectionOptions(max_attempts=1), cancel_event=cancel
            )
            self._start_progress("호스트 키 조회 완료", len(targets), len(checks))
            _abort_if_cancelled(cancel)
            self._review = {
                c.target.endpoint: c for c in checks if c.state is HostKeyTrustState.UNKNOWN
            }
            return {
                "checks": [
                    {
                        "endpoint": c.target.endpoint,
                        "state": c.state.value,
                        "key_type": c.observation.key_type,
                        "fingerprint": c.observation.fingerprint,
                        "known_fingerprint": c.known_fingerprint,
                        "message": c.message,
                    }
                    for c in checks
                ]
            }
        if action == "hostkey.approve":
            rows = body.get("checks", [body])
            if not isinstance(rows, list) or not 1 <= len(rows) <= len(self._review):
                raise ValueError("승인할 호스트 키를 선택하세요.")
            verified = []
            for row in rows:
                endpoint = row.get("endpoint") if isinstance(row, dict) else None
                check = self._review.get(endpoint) if isinstance(endpoint, str) else None
                if check is None or check.observation.fingerprint != row.get("fingerprint"):
                    raise ValueError("조회한 새 호스트 키의 대상과 지문을 다시 확인하세요.")
                verified.append(check)
            self.host_key_store.approve(verified)
            for check in verified:
                self._review.pop(check.target.endpoint, None)
            return {"approved": True}
        if action == "hostkey.list":
            return {
                "keys": [
                    {
                        "endpoint": entry.endpoint,
                        "key_type": entry.key_type,
                        "fingerprint": entry.fingerprint,
                        "approved_at": entry.approved_at,
                    }
                    for entry in self.host_key_store.list_approved()
                ]
            }
        if action == "hostkey.forget":
            rows = body.get("checks", [body])
            if not isinstance(rows, list) or not rows:
                raise ValueError("제거할 지문을 선택하세요.")
            selected = {}
            for row in rows:
                endpoint = row.get("endpoint") if isinstance(row, dict) else None
                fingerprint = row.get("fingerprint") if isinstance(row, dict) else None
                if (
                    not isinstance(endpoint, str)
                    or not isinstance(fingerprint, str)
                    or not fingerprint
                ):
                    raise ValueError("제거할 장비의 저장된 지문을 선택하세요.")
                selected[_endpoint(endpoint).endpoint] = fingerprint
            self.host_key_store.remove_reviewed(selected)
            self._review.clear()
            self.last_results = ()
            self.unverified_controls.clear()
            for endpoint in selected:
                session_legacy_ssh.revoke(_endpoint(endpoint))
            return {"removed": True}
        if action == "backup.history":
            extra = selected_directory(body.get("output_directory"))
            roots = (self.legacy_backup_dir, self.data_dir / "outputs")
            rows = backup_history(*roots, extra=extra)
            if extra is not None:
                roots += (extra,)
            return {
                "rows": self.history_folders.register(
                    "backup", rows, path_field="path", roots=roots
                )
            }
        if action == "history.open-folder":
            return self.history_folders.open(body.get("folder_id"))
        if action == "backup.pick-directory":
            return pick_directory(cancel)
        if action.startswith("profiles."):
            return self._profile(action, body)
        if self.credentials is None:
            raise ValueError("먼저 SSH 접속 정보를 입력하세요.")
        if action in {"ports.query", "ports.refresh"}:
            return self._query(
                _groups(body),
                self.credentials,
                cancel,
                refresh=action == "ports.refresh",
                target_text=body["targets"],
            )
        if action == "cable.test":
            if body.get("confirmed") is not True:
                raise ValueError("선택 포트의 링크 단절 가능성을 확인한 뒤 테스트를 실행하세요.")
            groups = _groups(body)
            total = sum(len(group.interfaces) for group in groups)
            self._start_progress("케이블 진단 중", total)
            options = CollectionOptions(
                max_attempts=1, command_timeout_seconds=35, max_output_bytes=64 * 1024
            )
            return diagnose_ports(
                groups,
                lambda target: self._cable_session(target, self.credentials, options, cancel),
                cancel,
                self._progress,
            )
        if action == "backup.run":
            return self._backup(body, self.credentials, cancel)
        raise ValueError("지원하지 않는 작업입니다.")

    def _query(self, groups, credentials, cancel, *, refresh: bool, target_text=None) -> dict:
        if refresh:
            current = {g.target: g for g in self._inventory}
            for group in groups:
                original = current.get(group.target)
                if original is None or not set(group.interfaces).issubset(original.interfaces):
                    raise ValueError("이 세션에서 조회했던 스위치와 포트만 재조회할 수 있습니다.")
            groups = tuple(
                PoETarget(g.target, g.interfaces, current[g.target].label) for g in groups
            )
        else:
            self._inventory = groups
            self.query_target_text = target_text
            self.last_results = ()
            self._unreported_ports.clear()
            self.unverified_controls.clear()
        requested = {(g.target, port) for g in groups for port in g.interfaces}
        # A failed refresh must not authorize actions on stale snapshots of those ports.
        retained = {
            (r.group.target, r.interface): r
            for r in self.last_results
            if (r.group.target, r.interface) not in requested
        }
        self.last_results = tuple(retained.values())
        observed = self._collect_ports(
            groups, credentials, cancel, "스위치 포트 조회 중", first_use_host_key_policy(cancel)
        )
        self._merge_query_results(groups, observed, cancel.is_set())
        for row in observed:
            if row.complete and row.status is QueryStatus.OK and row.snapshot:
                self.unverified_controls.discard((row.group.target, row.interface))
        result = self._port_view(requested)
        if not refresh:
            result["groups"] = self.inventory_view()
        return result

    def _collect_ports(self, groups, credentials, cancel, message, approve_host_key):
        total = sum(len(g.interfaces) for g in groups)
        self._start_progress(message, total)
        with self._progress_lock:
            self._query_events = []
            self._query_targets = groups
        _abort_if_cancelled(cancel)

        power_reads = set()
        completed_ports = set()

        def on_row(row):
            observation = matrix_observation(row)
            key = row.group.target, row.interface
            with self._progress_lock:
                self._query_events.append(observation)
                if row.snapshot is not None:
                    power_reads.add(key)
                if row.complete:
                    completed_ports.add(key)
                self.progress = {
                    "message": message,
                    "total": total,
                    "completed": len(completed_ports),
                    "powerRead": len(power_reads),
                }

        return self.query_service.collect(
            groups,
            credentials,
            approve_host_key=approve_host_key,
            cancel_event=cancel,
            on_result=on_row,
        )

    def refresh_control_power(self, action, outcomes, credentials, cancel):
        """Observe a completed PoE operation without ever reentering the write path."""
        labels = {g.target: g.label for g in self._inventory}
        inventory = {(g.target, port) for g in self._inventory for port in g.interfaces}
        keys = [
            key
            for key, status in outcomes.items()
            if status in {"verified", "unchanged", "unverified"}
        ]
        failed = [
            key
            for key, status in outcomes.items()
            if status == "preflight_failed" and key in inventory
        ]
        if not (keys or failed):
            return {}

        def groups_for(selected):
            ports = {}
            for target, interface in selected:
                ports.setdefault(target, []).append(interface)
            return tuple(
                PoETarget(t, tuple(items), labels.get(t, "")) for t, items in ports.items()
            )

        def collect(selected, message):
            try:
                rows = self._collect_ports(
                    groups_for(selected), credentials, cancel, message, lambda _check: False
                )
            except Exception:
                # Preserve the independent control result if SSH collection cannot complete.
                return {}
            selected_keys = set(selected)
            received = {}
            for row in rows:
                key = row.group.target, row.interface
                if key not in selected_keys or not row.complete:
                    continue
                if row.snapshot and (row.snapshot.target, row.snapshot.interface) != key:
                    continue
                received[key] = row
            return received

        observed = collect(keys, "PoE 제어 후 전력 조회 중") if keys else {}
        retry = []
        for key in keys:
            row = observed.get(key)
            if (
                outcomes[key] != "verified"
                or not row
                or row.status is not QueryStatus.OK
                or not row.snapshot
            ):
                continue
            watts = row.snapshot.watts
            if watts is not None and (
                (action is PortAction.POE_ON and watts == 0)
                or (action is PortAction.POE_OFF and watts > 0)
            ):
                retry.append(key)
        if retry and not cancel.is_set():
            self._start_progress(f"포트 {len(retry)}개 전력 변화 대기 중", 0)
            if not cancel.wait(POWER_SETTLE_SECONDS):
                rechecked = collect(retry, "PoE 전력 변화 재조회 중")
                for key in retry:
                    observed.pop(key, None)
                observed.update(rechecked)

        readings = []
        invalid = set()
        for target, interface in keys:
            row = observed.get((target, interface))
            if (
                not row
                or row.status not in {QueryStatus.OK, QueryStatus.UNSUPPORTED}
                or not row.snapshot
            ):
                invalid.add((target, interface))
            snapshot = row.snapshot if row and row.status is QueryStatus.OK else None
            watts = snapshot.watts if snapshot else None
            status, message = "measured", ""
            if watts is None:
                cancelled = row.status is QueryStatus.CANCELLED if row else cancel.is_set()
                status = "cancelled" if cancelled else "unavailable"
                message = (row.message if row else "") or (
                    "전력 조회가 중지되었습니다."
                    if cancelled
                    else "새 전력 측정값을 받지 못했습니다."
                )
            readings.append(
                dict(
                    ip=target.ip,
                    sshPort=target.port,
                    port=interface,
                    watts=watts,
                    observed=snapshot.observed_at if watts is not None else "",
                    status=status,
                    message=message,
                )
            )
        result = {"powerRefresh": {"readings": readings}}
        registered = [key for key in keys if key in inventory] + failed
        if registered:
            # Never add a manually inspected, unregistered port to the inventory.
            self._merge_query_results(
                groups_for(registered), tuple(observed.values()), cancel.is_set()
            )
            self._unreported_ports.update(invalid.intersection(registered))
            for key in registered:
                if outcomes[key] == "unverified":
                    self.unverified_controls.add(key)
            result.update(self._port_view(registered))
        return result

    def _merge_query_results(self, groups, observed, cancelled):
        """Fill only requested gaps with a terminal failure that cannot authorize a write."""
        rows = {(r.group.target, r.interface): r for r in self.last_results}
        returned = {(r.group.target, r.interface): r for r in observed}
        missing = []
        finished = datetime.now(UTC).isoformat()
        for group in groups:
            for port in group.interfaces:
                key = group.target, port
                row = returned.get(key)
                if row is None:
                    row = PoEQueryResult(
                        group,
                        port,
                        QueryStatus.CANCELLED if cancelled else QueryStatus.FAILED,
                        error_code=ErrorCode.CANCELLED if cancelled else ErrorCode.UNEXPECTED_ERROR,
                        message="조회가 중지되어 새 측정값이 없습니다."
                        if cancelled
                        else "이번 조회의 포트 측정 결과를 받지 못했습니다.",
                        finished_at=finished,
                    )
                    missing.append(row)
                    self._unreported_ports.add(key)
                else:
                    self._unreported_ports.discard(key)
                rows[key] = row
        ordered = ((g.target, port) for g in self._inventory for port in g.interfaces)
        self.last_results = tuple(rows[key] for key in ordered if key in rows)
        self._unreported_ports.intersection_update(rows)
        return missing

    def invalidate_failed_refresh(self, cancelled):
        """Drop measurements for the accepted request if no publishable result exists."""
        if not self._query_targets:
            return []
        absent = self._merge_query_results(self._query_targets, (), cancelled)
        return [
            dict(
                matrix_observation(row),
                eligible_operations=[PortAction.DISABLE.value],
                controlUsed=True,
                refreshInvalidated=True,
            )
            for row in absent
        ]

    def _run_dir(self) -> Path:
        base = self.data_dir / "outputs"
        now = datetime.now()
        # The desktop writer may advance into the next day for same-second collisions.
        for day in (now, now + timedelta(days=1)):
            expected = base / day.strftime("%Y-%m-%d")
            if expected.resolve() != expected:
                raise ValueError("결과를 저장할 데이터 폴더의 경로를 확인하세요.")
        return create_run_directory(base, now=now)

    def _file(self, path: Path) -> dict:
        return {"id": path.resolve().relative_to(self.data_dir).as_posix(), "name": path.name}

    def _profile(self, action: str, body: dict) -> dict:
        if action == "profiles.export":
            groups = _groups(body)
            path = self._run_dir() / "port-targets.json"
            save_target_profile(path, groups)
            return {"files": [self._file(path)]}
        if action != "profiles.import":
            raise ValueError("지원하지 않는 대상 목록 작업입니다.")
        text = body.get("text")
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_PROFILE_BYTES:
            raise ValueError("대상 프로필 JSON은 1 MiB 이하로 입력하세요.")
        with tempfile.TemporaryDirectory(dir=self.data_dir) as directory:
            path = Path(directory) / "targets.json"
            path.write_text(text.removeprefix("\ufeff"), encoding="utf-8")
            groups = load_target_profile(path)
        return {"targets": target_table(groups)}

    def _port_view(self, sampled):
        # A partial refresh returns other ports too. Identify the ports sampled by this operation.
        return {
            "rows": self._matrix_rows(),
            "_sampledPorts": [(target.ip, target.port, port) for target, port in sampled],
        }

    def _matrix_rows(self):
        rows = []
        for native in self.last_results:
            key = native.group.target, native.interface
            locked = key in self.unverified_controls
            allowed = (
                [PortAction.DISABLE.value]
                if locked or key in self._unreported_ports
                else _eligible(native)
            )
            values = matrix_observation(native)
            values.update(
                eligible_operations=allowed, eligibleOff=PortAction.POE_OFF.value in allowed
            )
            if key in self._unreported_ports:
                values.update(controlUsed=True, refreshInvalidated=True)
            elif locked:
                values.update(
                    controlUsed=True,
                    powerObserved=native.status is QueryStatus.OK
                    and native.snapshot is not None
                    and native.snapshot.watts is not None,
                )
            rows.append(values)
        return rows

    def _backup(self, body: dict, credentials: Credentials, cancel: threading.Event) -> dict:
        targets = _backup_targets(body)
        mode = FilenameMode(body.get("filename_mode", "hostname_ip"))
        destination = selected_directory(body.get("output_directory"))
        options = CollectionOptions(
            concurrency=_bounded_integer(body, "concurrency", 10, 20),
            max_attempts=_bounded_integer(body, "max_attempts", 4, 4),
        )
        run_dir = self._run_dir()
        started = datetime.now(UTC)
        log_path = run_dir / "operation.jsonl"
        secrets = [
            credentials.username,
            credentials.password,
            credentials.enable_secret or "",
            *(t.ip for t in targets),
        ]
        logger = SanitizedJsonlLogger(log_path, sensitive_values=secrets)
        warnings = list(backup_storage_permission_warnings(run_dir))
        log_error = False
        log_lock = threading.Lock()

        def log(event=None, **fields):
            nonlocal log_error
            with log_lock:
                if not log_error:
                    try:
                        logger.log(event, **fields)
                    except OSError:
                        log_error = True
                        warnings.append(
                            "진단 로그를 저장하지 못했습니다. 설정 파일과 결과 보고서를 계속 저장합니다."
                        )

        self.collector.begin_run()
        log(stage="started", message="Configuration backup started.")
        progress = BackupProgress(self, targets)

        def on_key_event(event):
            progress.record(event, "host_key")
            log(event, phase="host_key")

        # Make a fresh unauthenticated observation. Only trusted targets proceed to the
        # collector, which checks the actual connection's key again before authentication.
        try:
            checks = self.collector.probe_host_keys(
                targets, options=options, cancel_event=cancel, on_event=on_key_event
            )
        except CollectionFailure as exc:
            if exc.code is not ErrorCode.CANCELLED:
                raise
            checks = []
        for index, check in enumerate(checks):
            if cancel.is_set():
                break
            if check.target != targets[index]:
                raise CollectionFailure(
                    ErrorCode.HOST_KEY_REJECTED, "SSH 지문 조회 대상이 일치하지 않습니다."
                )
            if check.state is HostKeyTrustState.UNKNOWN:
                try:
                    self.host_key_store.approve([check])
                    checks[index] = replace(
                        check,
                        state=HostKeyTrustState.TRUSTED,
                        known_fingerprint=check.observation.fingerprint,
                        message="장비 지문 자동 등록 완료",
                    )
                except CollectionFailure as exc:
                    state = (
                        HostKeyTrustState.CHANGED
                        if exc.code is ErrorCode.HOST_KEY_CHANGED
                        else HostKeyTrustState.REJECTED
                    )
                    checks[index] = replace(
                        check, state=state, error_code=exc.code, message=exc.safe_message
                    )
        observations = {check.target: check for check in checks}
        trusted = [c.target for c in checks if c.state is HostKeyTrustState.TRUSTED]
        progress.host_keys(checks)

        def on_event(event):
            progress.record(event, "backup")
            log(event, phase="backup")

        collected = {
            r.target: r
            for r in self.collector.collect_many(
                trusted, credentials, options, cancel_event=cancel, on_event=on_event
            )
        }
        self._start_progress("구성 파일·보고서 저장 중", len(targets), len(targets))
        files = []
        results = []
        for target in targets:
            check = observations.get(target)
            result = collected.get(target)
            if result is None:
                finished = datetime.now(UTC)
                cancelled = cancel.is_set()
                code = (
                    ErrorCode.CANCELLED
                    if cancelled
                    else (check.error_code if check else None) or ErrorCode.HOST_KEY_REJECTED
                )
                if not cancelled and check and check.state is HostKeyTrustState.CHANGED:
                    code = ErrorCode.HOST_KEY_CHANGED
                result = DeviceResult(
                    target,
                    DeviceStatus.CANCELLED if cancelled else DeviceStatus.FAILED,
                    0,
                    started,
                    finished,
                    (finished - started).total_seconds(),
                    error_code=code,
                    error_message="작업을 취소했습니다."
                    if cancelled
                    else (check.message if check else "호스트 키 확인 실패"),
                    failure_phase=DiagnosticPhase.HOST_KEY,
                )
                if check and check.retry_exhausted:
                    result.status = DeviceStatus.RETRY_EXHAUSTED
            result.host_key_attempts = (
                check.attempts if check else progress.rows[target]["host_key_attempts"]
            )
            if result.succeeded and result.config_text is not None:
                try:
                    path = device_config_path(
                        run_dir, hostname=result.hostname, ip_address=target.ip, filename_mode=mode
                    )
                    stored = write_config_atomic(path, result.config_text)
                    result.config_path, result.config_sha256 = stored.path, stored.sha256
                    files.append(self._file(stored.path))
                except OSError:
                    result.status, result.error_code = (
                        DeviceStatus.FAILED,
                        ErrorCode.REPORT_WRITE_FAILED,
                    )
                    result.error_message = "설정 파일을 저장하지 못했습니다."
                    result.failure_phase, result.diagnostic_detail = (
                        DiagnosticPhase.REPORT_STORAGE,
                        DiagnosticDetail.OS_ERROR,
                    )
                    result.config_sha256 = None
                finally:
                    result.config_text = None
            result.diagnostic_code = diagnostic_code_for_result(result)
            results.append(result)
        for code, count in Counter(r.diagnostic_code for r in results if r.diagnostic_code).items():
            log(stage="diagnostic_summary", diagnostic_code=code, count=count)
        log(stage="finished", status="cancelled" if cancel.is_set() else "finished")
        if log_path.is_file():
            files.append(self._file(log_path))
        try:
            report = write_result_workbook(
                run_dir,
                results,
                summary=ReportSummary(started, datetime.now(UTC), cancel.is_set(), tuple(warnings)),
            )
            files.append(self._file(report))
        except OSError, ValueError:
            warnings.append("결과 엑셀을 저장하지 못했습니다. 개별 설정 파일을 확인하세요.")
        output = run_dir
        if destination is not None:
            self._start_progress("선택한 폴더에 백업 파일 복사 중", len(targets), len(targets))
            output, copy_warnings = copy_backup_files(
                destination, run_dir, [f["name"] for f in files]
            )
            warnings.extend(copy_warnings)
            if output != run_dir:
                warnings.extend(
                    message
                    for message in backup_storage_permission_warnings(output)
                    if message not in warnings
                )
        return {
            "rows": [
                {
                    "endpoint": r.target.endpoint,
                    "hostname": r.hostname or "",
                    "status": r.status.value,
                    "message": r.error_message,
                    "diagnostic_code": r.diagnostic_code,
                    "attempts": r.attempts,
                    "host_key_attempts": r.host_key_attempts,
                    "error_code": r.error_code.value if r.error_code else "",
                    "model": r.model or "",
                    "sku": r.sku or "",
                }
                for r in results
            ],
            "files": files,
            "warnings": warnings,
            "output_directory": str(output),
        }

    @contextmanager
    def _cable_session(self, target, credentials, options, cancel):
        session = None
        try:
            _abort_if_cancelled(cancel)
            observation = self.collector.host_key_probe.probe(
                target, timeout=options.connect_timeout_seconds
            )
            _abort_if_cancelled(cancel)
            if observation.target != target:
                raise CollectionFailure(
                    ErrorCode.HOST_KEY_REJECTED, "SSH 지문 조회 대상이 일치하지 않습니다."
                )
            check = self.host_key_store.check(observation)
            if check.state is HostKeyTrustState.UNKNOWN:
                _abort_if_cancelled(cancel)
                self.host_key_store.approve([check])
            self.host_key_store.require_trusted(observation)
            _abort_if_cancelled(cancel)
            session = CableSession(target, credentials, options, self.host_key_store)
            session.connect()
            _abort_if_cancelled(cancel)
            if credentials.enable_secret or not session.get_prompt().endswith("#"):
                session.enter_enable()
                _abort_if_cancelled(cancel)
            session.disable_paging()
            _abort_if_cancelled(cancel)
            session.set_terminal_width()
            _abort_if_cancelled(cancel)
            version = session.send_show("show version", cancel_event=cancel)
            _abort_if_cancelled(cancel)
            try:
                modules = session.send_show("show modules", cancel_event=cancel)
            except CollectionFailure as exc:
                if exc.code is not ErrorCode.COMMAND_REJECTED:
                    raise
                modules = ""
            validate_device_identity(version, modules)
            _abort_if_cancelled(cancel)
            yield session
        finally:
            if session is not None:
                with suppress(Exception):
                    session.close()

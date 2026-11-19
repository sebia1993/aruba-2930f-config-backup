"""Offline target editing and the native per-port matrix projection."""

import csv
import io

from aruba2930f_backup.port_status_mapper import STATE_LABELS, make_matrix_port

from aruba2930f_backup.poe_query_models import MAX_TARGETS, PoETarget
from aruba2930f_backup.poe_targets import merge_targets


def group_targets(groups):
    return [
        {
            "ip": group.target.ip,
            "ssh_port": group.target.port,
            "interfaces": list(group.interfaces),
            "label": group.label,
        }
        for group in groups
    ]


def _editor_groups(rows, parse):
    if not isinstance(rows, list) or len(rows) > MAX_TARGETS:
        raise ValueError("스위치 행은 최대 300개입니다.")
    groups = []
    for index, row in enumerate(rows, 1):
        try:
            if not isinstance(row, dict):
                raise ValueError("스위치 행을 확인하세요.")
            ip = row.get("ip")
            port = row.get("ssh_port", 22)
            interfaces = row.get("interfaces")
            if not isinstance(ip, str) or not isinstance(interfaces, str) or not ip.strip():
                raise ValueError("스위치 주소와 포트 목록을 입력하세요.")
            if (
                any(s.isspace() for s in ip.strip())
                or ":" in ip
                or "\n" in interfaces
                or "\r" in interfaces
            ):
                raise ValueError("스위치 주소와 포트 목록을 확인하세요.")
            if (
                not isinstance(port, (int, str))
                or not str(port).isascii()
                or not str(port).isdigit()
            ):
                raise ValueError("SSH 포트를 확인하세요.")
            (group,) = parse({"targets": f"{ip.strip()}:{port} {interfaces.strip()}"})
            groups.append(PoETarget(group.target, group.interfaces, row.get("label", "")))
        except ValueError as exc:
            raise ValueError(f"{index}행 · {exc}") from exc
    return merge_targets(groups)


def preview_targets(body, parse):
    mode = body.get("mode", "replace")
    if mode not in ("replace", "append"):
        raise ValueError("대상 적용 방식을 선택하세요.")
    incoming = _editor_groups(body["rows"], parse) if "rows" in body else parse(body)
    existing = body.get("existing", "")
    if mode == "append" and existing:
        incoming = merge_targets((*parse({"targets": existing}), *incoming))
    table = io.StringIO(newline="")
    writer = csv.writer(table, dialect="excel-tab")
    if incoming:
        writer.writerow(["IP", "ssh_port", "interfaces", "label"])
        for g in incoming:
            writer.writerow([g.target.ip, g.target.port, ",".join(g.interfaces), g.label])
    return {
        "targets": table.getvalue(),
        "groups": group_targets(incoming),
        "port_count": sum(len(g.interfaces) for g in incoming),
    }


def matrix_fields(result):
    item = make_matrix_port(result.group, result.interface, result)
    fields = {
        "matrix_state": item.state.value,
        "matrix_label": STATE_LABELS[item.state],
        "matrix_note": item.note,
    }
    if result.identity:
        identity = result.identity
        sources = {
            "system-information": "System Name (장비 직접 조회)",
            "prompt": "SSH 프롬프트",
            "running-config": "설정 백업 Hostname",
        }
        fields.update(
            sku=identity.sku or "",
            software_version=identity.software_version or "",
            hostname_source=sources.get(identity.hostname_source, "확인 불가"),
        )
    return fields


def matrix_observation(result):
    item = make_matrix_port(result.group, result.interface, result)
    snap = result.snapshot
    assessment = result.assessment
    lldp = result.lldp
    return {
        "ip": result.group.target.ip,
        "ssh_port": result.group.target.port,
        "interface": result.interface,
        "label": result.group.label,
        "hostname": item.switch,
        "status": result.status.value,
        "complete": result.complete,
        "error_code": result.error_code.value if result.error_code else "",
        "message": result.message,
        "matrix_state": item.state.value,
        "matrix_label": STATE_LABELS[item.state],
        "matrix_note": item.note,
        "watts": snap.watts if snap else None,
        "poe_enabled": snap.poe_enabled if snap else None,
        "poe_status": snap.poe_status if snap else "",
        "assessment_status": assessment.status.value if assessment else "",
        "assessment_reason": assessment.reason if assessment else "",
        "requested_watts": str(assessment.requested_watts)
        if assessment and assessment.requested_watts is not None
        else None,
        "allocated_watts": str(assessment.allocated_watts)
        if assessment and assessment.allocated_watts is not None
        else None,
        "power_evidence": {"issues": result.power_evidence.issues}
        if result.power_evidence
        else None,
        "lldp_status": lldp.status.value if lldp else "",
        "lldp_error_code": lldp.error_code.value if lldp and lldp.error_code else "",
        "lldp_neighbors": [
            {
                "system_name": neighbor.system_name,
                "system_description": neighbor.system_description,
                "model": neighbor.model,
            }
            for neighbor in lldp.neighbors
        ]
        if lldp
        else [],
        "link_status": item.link,
        "link_observed_at": result.link_observed_at,
        "ap_names": item.ap,
        "observed_at": item.observed_at,
    }

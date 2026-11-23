"""Offline target editing and the native per-port matrix projection."""

import csv
import io
from dataclasses import asdict
from decimal import Decimal

from aruba2930f_backup.poe_power import AP535_REFERENCE_ID, AP535_REFERENCE_URL
from aruba2930f_backup.poe_query_models import MAX_TARGETS, PoETarget, QueryStatus
from aruba2930f_backup.poe_targets import merge_targets
from aruba2930f_backup.port_power_diagnosis import diagnose_port_power
from aruba2930f_backup.port_status_mapper import STATE_LABELS, make_matrix_port


def group_targets(groups, all_endpoints=()):
    return [
        {
            "ip": group.target.ip,
            "ssh_port": group.target.port,
            "interfaces": list(group.interfaces),
            "label": group.label,
            **({"all_ports": True} if group.all_ports or group.target in all_endpoints else {}),
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
            interfaces = row.get("interfaces", "")
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
            groups.append(
                PoETarget(
                    group.target, group.interfaces, row.get("label", ""), all_ports=group.all_ports
                )
            )
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
    return {
        "targets": target_table(incoming),
        "groups": group_targets(incoming),
        "port_count": None
        if any(g.all_ports for g in incoming)
        else sum(len(g.interfaces) for g in incoming),
    }


def target_table(groups):
    if not groups:
        return ""
    table = io.StringIO(newline="")
    writer = csv.writer(table, dialect="excel-tab")
    writer.writerow(["IP", "ssh_port", "interfaces", "label"])
    for group in groups:
        writer.writerow(
            [group.target.ip, group.target.port, ",".join(group.interfaces), group.label]
        )
    return table.getvalue()


def _evidence_fields(pairs):
    # Store exact decimal power values as text within the structured evidence.
    return {k: format(v, "f") if isinstance(v, Decimal) else v for k, v in pairs}


def matrix_observation(result):
    item = make_matrix_port(result.group, result.interface, result)
    snap = result.snapshot
    assessment = result.assessment
    lldp = result.lldp
    identity = result.identity
    diagnosis = diagnose_port_power(snap)
    values = {
        "ip": result.group.target.ip,
        "ssh_port": result.group.target.port,
        "interface": result.interface,
        "label": result.group.label,
        "hostname": (snap.hostname if snap else "")
        or (identity.hostname or "" if identity else ""),
        "model": (snap.model if snap else "") or (identity.model if identity else ""),
        "status": result.status.value,
        "complete": result.complete,
        "error_code": result.error_code.value if result.error_code else "",
        "message": result.message,
        "matrix_state": item.state.value,
        "matrix_label": STATE_LABELS[item.state],
        "matrix_note": item.note,
        "enabled": snap.enabled if snap else None,
        "watts": snap.watts if snap and result.status is QueryStatus.OK else None,
        "poe_enabled": snap.poe_enabled if snap else None,
        "poe_status": snap.poe_status if snap else "",
        "assessment_status": assessment.status.value if assessment else "",
        "assessment_label": assessment.label if assessment else "",
        "assessment_reason": assessment.reason if assessment else "",
        "assessment_model": assessment.model if assessment else "",
        "assessment_model_source": assessment.model_source if assessment else "",
        "negotiation": assessment.negotiation if assessment else "",
        "reference_id": assessment.reference_id if assessment else "",
        "reference_url": AP535_REFERENCE_URL
        if assessment and assessment.reference_id == AP535_REFERENCE_ID
        else "",
        "port_poe_state": diagnosis.label,
        "port_poe_explanation": diagnosis.reason,
        "requested_watts": str(assessment.requested_watts)
        if assessment and assessment.requested_watts is not None
        else None,
        "allocated_watts": str(assessment.allocated_watts)
        if assessment and assessment.allocated_watts is not None
        else None,
        "power_evidence": asdict(result.power_evidence, dict_factory=_evidence_fields)
        if result.power_evidence
        else None,
        "lldp_status": lldp.status.value if lldp else "",
        "lldp_observed_at": lldp.observed_at if lldp else "",
        "lldp_message": lldp.message if lldp else "",
        "lldp_error_code": lldp.error_code.value if lldp and lldp.error_code else "",
        "lldp_neighbors": [
            asdict(neighbor, dict_factory=_evidence_fields) for neighbor in lldp.neighbors
        ]
        if lldp
        else [],
        "link_status": result.observed_link,
        "link_observed_at": result.link_observed_at,
        "ap_names": " | ".join(n.system_name for n in lldp.neighbors if n.system_name)
        if lldp
        else "",
        "observed_at": snap.observed_at if snap else "",
        "poe_finished_at": result.poe_finished_at,
        "finished_at": result.finished_at,
    }
    if identity:
        sources = {
            "system-information": "System Name (장비 직접 조회)",
            "prompt": "SSH 프롬프트",
            "running-config": "설정 백업 Hostname",
        }
        values.update(
            sku=identity.sku or "",
            software_version=identity.software_version or "",
            hostname_source=sources.get(identity.hostname_source, "확인 불가"),
        )
    return values

"""Operator-owned Ping profiles and exports of server-observed general results."""

import json
import sqlite3
import tempfile
import unicodedata
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

from aruba2930f_backup.poe_targets import MAX_PROFILE_BYTES, load_target_profile

from .ping_tools import PingPlan, parse_destinations, unicast_ipv4

PROFILE_TYPE = "aruba-local-web-ping"
COLUMNS = {
    "index": "조합 번호 (0부터)",
    "ip": "스위치 IPv4",
    "ssh_port": "SSH 포트",
    "hostname": "호스트명",
    "destination_ip": "목적지 IPv4",
    "destination_label": "서버 이름",
    "source_mode": "Source 모드",
    "requested_source": "요청 Source",
    "verified_source_ip": "확인된 Source IP",
    "sent": "전송",
    "received": "응답",
    "loss_pct": "손실률 (%)",
    "rtt_min_ms": "최소 RTT (ms)",
    "rtt_avg_ms": "평균 RTT (ms)",
    "rtt_max_ms": "최대 RTT (ms)",
    "rtt_evidence": "RTT 근거",
    "status": "상태",
    "reason_code": "오류 사유",
    "slot_started_at": "관측 시작 (UTC)",
    "slot_finished_at": "관측 종료 (UTC)",
    "duration_ms": "슬롯 소요 (ms)",
}


def validated_profile(value):
    plan = PingPlan.parse(value)
    return {
        "targets": "\n".join(t.endpoint for t in plan.targets),
        "destinations": [asdict(d) for d in plan.destinations],
        "options": asdict(plan.options),
        "source_ips": {t.endpoint: ip for t, ip in plan.source_ips},
    }


def profile_value(scope, value):
    if scope == "destinations":
        return [asdict(d) for d in parse_destinations(value)]
    return validated_profile(value)


def _name(value):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 80
        or any(unicodedata.category(c).startswith("C") for c in value)
    ):
        raise ValueError("프로필 이름을 1~80자로 입력하세요.")
    return value.strip()


def import_profile(text, directory):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ValueError("프로필 JSON은 1 MiB 이하로 입력하세요.")
    text = text.removeprefix("\ufeff")
    doc = json.loads(text)
    if isinstance(doc, dict) and doc.get("format") == PROFILE_TYPE:
        scope = "destinations" if "destinations" in doc else "plan"
        if (
            set(doc) != {"format", "version", scope}
            or type(doc["version"]) is not int
            or doc["version"] != 1
        ):
            raise ValueError("지원하지 않는 Ping 프로필입니다.")
        return {scope: profile_value(scope, doc[scope])}
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        path = Path(tmp) / "import.json"
        path.write_text(text, encoding="utf-8")
        groups = load_target_profile(path)
    endpoints = dict.fromkeys(g.target.endpoint for g in groups)
    if not 1 <= len(endpoints) <= 300:
        raise ValueError("스위치는 1~300대를 지정하세요.")
    for g in groups:
        unicast_ipv4(g.target.ip)
    return {"targets": "\n".join(endpoints)}


def profile_request(jobs, action, data):
    if action == "import" and set(data) == {"text"}:
        return import_profile(data["text"], jobs.directory)
    data = dict(data)
    scope = data.pop("scope", "plan")
    if scope not in ("plan", "destinations"):
        raise ValueError("프로필 범위를 확인하세요.")
    table = "ping_profiles" if scope == "plan" else "ping_destination_profiles"
    if action == "portable" and set(data) == {scope}:
        return {"format": PROFILE_TYPE, "version": 1, scope: profile_value(scope, data[scope])}
    with closing(sqlite3.connect(jobs.db)) as db, db:
        db.execute(f"CREATE TABLE IF NOT EXISTS {table}(name TEXT PRIMARY KEY, plan TEXT NOT NULL)")
        if action == "list" and not data:
            return {
                "names": [row[0] for row in db.execute(f"SELECT name FROM {table} ORDER BY name")]
            }
        name = _name(data.get("name"))
        if action == "load" and set(data) == {"name"}:
            row = db.execute(f"SELECT plan FROM {table} WHERE name=?", (name,)).fetchone()
            if row is None:
                raise KeyError(name)
            return {scope: profile_value(scope, json.loads(row[0]))}
        if action == "save" and set(data) == {"name", scope}:
            payload = json.dumps(profile_value(scope, data[scope]), ensure_ascii=False)
            db.execute("BEGIN IMMEDIATE")
            if db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= 200:
                raise ValueError("최대 200개까지 저장할 수 있습니다.")
            try:
                db.execute(f"INSERT INTO {table} VALUES (?,?)", (name, payload))
            except sqlite3.IntegrityError as exc:
                raise ValueError("이미 저장한 이름입니다. 새 이름을 입력하세요.") from exc
            return {"name": name}
        if action == "delete" and set(data) == {"name"}:
            db.execute(f"DELETE FROM {table} WHERE name=?", (name,))
            return {"name": name}
    raise ValueError("지원하지 않는 프로필 요청입니다.")


def _append(sheet, values):
    cells = []
    for value in values:
        if isinstance(value, str):
            cell = WriteOnlyCell(sheet, value=value)
            cell.data_type = "s"
            cells.append(cell)
        else:
            cells.append(value)
    sheet.append(cells)


def general_excel(jobs, key, data):
    if set(data) != {"indices"} or not isinstance(data["indices"], list):
        raise ValueError("내보낼 조합을 선택하세요.")
    indices = data["indices"]
    if not 1 <= len(indices) <= 3000 or any(type(i) is not int or i < 0 for i in indices):
        raise ValueError("내보낼 조합 번호를 확인하세요.")
    want = set(indices)
    if len(want) != len(indices):
        raise ValueError("조합 번호가 중복되었습니다.")
    job = jobs.ping_status(key)
    result = job["result"]
    if job["state"] == "running" or not result or max(want) >= result["completed"]:
        raise ValueError("완료한 작업에서 측정 결과를 선택하세요.")
    rows = list(result["rows"])
    while len(rows) < result["completed"]:
        page = jobs.ping_status(key, len(rows))["result"]["rows"]
        if not page:
            raise ValueError("결과 페이지를 다시 확인하세요.")
        rows.extend(page)
    chosen = [row for row in rows if row["index"] in want]
    if len(chosen) != len(want):
        raise ValueError("요청한 조합이 없습니다.")
    book = Workbook(write_only=True)
    summary = book.create_sheet("요약")
    for pair in [
        ("작업 ID", key),
        ("상태", job["state"]),
        ("시작 (UTC)", job["started"]),
        ("종료 (UTC)", job["finished"]),
        ("선택 조합", len(chosen)),
        ("전체 조합", result["total"]),
    ]:
        _append(summary, pair)
    for name, value in result.get("options", {}).items():
        _append(summary, [name, value])
    _append(summary, ["목적지", "상태", "선택 범위 조합 수"])
    for (ip, status), count in sorted(
        Counter((r["destination_ip"], r["status"]) for r in chosen).items()
    ):
        _append(summary, [ip, status, count])
    detail = book.create_sheet("상세 결과")
    _append(detail, COLUMNS.values())
    for row in chosen:
        _append(detail, [row.get(field) for field in COLUMNS])
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()

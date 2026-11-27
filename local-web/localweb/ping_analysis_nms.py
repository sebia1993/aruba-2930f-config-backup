"""Operator-mapped NMS alarms from bounded local CSV/XLSX uploads."""

import base64
import binascii
import csv
import hashlib
import io
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import PureWindowsPath
from zipfile import ZipFile

from openpyxl import load_workbook

MAX_BYTES = 3 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLS = 80
KST = timezone(timedelta(hours=9))
_ZONES = {"UTC": UTC, "KST": KST}
_KEYS = ("device", "message", "opened", "closed")
_FORMULA = object()


def _sheet_rows(raw, sheet):
    with ZipFile(io.BytesIO(raw)) as archive:
        files = archive.infolist()
        if len(files) > 300 or sum(part.file_size for part in files) > 20 * 1024**2:
            raise ValueError("Excel 파일의 압축 해제 크기 또는 항목 수가 한도를 초과했습니다.")
        for part in files:
            name = part.filename.lower()
            if part.flag_bits & 1 or "vbaproject" in name or "externallinks/" in name:
                raise ValueError("매크로·외부 연결·암호화 파일은 지원하지 않습니다.")
            if name.endswith((".xml", ".rels")):
                xml = archive.read(part).lower().replace(b"\x00", b"")
                if b"<!doctype" in xml or b"<!entity" in xml:
                    raise ValueError("지원하지 않는 Excel XML 형식입니다.")
    book = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    try:
        names = [ws.title for ws in book.worksheets]
        if not names or (sheet is not None and sheet not in names):
            raise ValueError("가져올 워크시트를 확인하세요.")
        ws = book[sheet or names[0]]
        # Excel dimensions can be missing or misleading, stream and police actual cells.
        ws.reset_dimensions()
        rows = []
        for row in ws.iter_rows():
            if len(rows) > MAX_ROWS or len(row) > MAX_COLS:
                raise ValueError("시트는 헤더 외 5,000행, 80열까지 지원합니다.")
            rows.append([_FORMULA if cell.data_type == "f" else cell.value for cell in row])
        return names, rows
    finally:
        book.close()


def unpack_file(body):
    name = body.get("name")
    data = body.get("file")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 255
        or PureWindowsPath(name).name != name
        or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name)
        or not _xml_text(name)
    ):
        raise ValueError("파일 이름을 확인하세요.")
    sheet = body.get("sheet")
    if sheet is not None and (
        not isinstance(sheet, str) or len(sheet) > 31 or not _xml_text(sheet)
    ):
        raise ValueError("시트 이름을 확인하세요.")
    if not isinstance(data, str) or len(data) > 4 * ((MAX_BYTES + 2) // 3):
        raise ValueError("파일은 3 MiB 이하로 선택하세요.")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("파일 데이터를 해석할 수 없습니다.") from exc
    if len(raw) > MAX_BYTES:
        raise ValueError("파일은 3 MiB 이하로 선택하세요.")
    suffix = PureWindowsPath(name).suffix.lower()
    if suffix == ".xlsx":
        try:
            sheets, rows = _sheet_rows(raw, body.get("sheet"))
        except ValueError:
            raise
        except Exception as exc:
            # XML parsers, ZIP decoders and Excel validators have different exceptions.
            raise ValueError("Excel 파일을 읽지 못했습니다.") from exc
    elif suffix == ".csv":
        if sheet:
            raise ValueError("CSV에는 워크시트가 없습니다.")
        try:
            encoding = body.get("encoding", "utf-8-sig")
            if encoding not in ("utf-8-sig", "cp949"):
                raise ValueError("CSV 문자 인코딩을 확인하세요.")
            source = raw.decode(encoding)
            rows = []
            for row in csv.reader(io.StringIO(source, newline=""), strict=True):
                if len(rows) > MAX_ROWS or len(row) > MAX_COLS:
                    raise ValueError("CSV는 헤더 외 5,000행, 80열까지 지원합니다.")
                rows.append(row)
            sheets = []
        except (UnicodeError, csv.Error) as exc:
            raise ValueError("CSV 구분자와 문자 인코딩을 확인하세요.") from exc
    else:
        raise ValueError("CSV 또는 XLSX 파일을 선택하세요.")
    if not rows or not rows[0] or len(rows) < 2:
        raise ValueError("첫 행에 열 이름이 있고 하나 이상의 데이터 행이 필요합니다.")
    headers = [str(value or "")[:100] if value is not _FORMULA else "" for value in rows[0]]
    return name, hashlib.sha256(raw).hexdigest(), sheets, headers, rows[1:]


def _timestamp(value, zone):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and len(value.strip()) <= 60:
        value = value.strip()
        if not re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}", value):
            raise ValueError("날짜/시각 형식을 확인하세요.")
        dt = datetime.fromisoformat(value.replace("/", "-"))
    else:
        raise ValueError("날짜 및 시각이 필요합니다.")
    if not 1970 <= dt.year <= 9900:
        raise ValueError("연도를 확인하세요.")
    if dt.tzinfo is None:
        if zone not in _ZONES:
            raise ValueError("시간대 없는 시각은 UTC 또는 KST를 지정하세요.")
        dt = dt.replace(tzinfo=_ZONES[zone])
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def _xml_text(s):
    return not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", s)


def _string(value, maximum):
    if not isinstance(value, str):
        raise ValueError("장비 식별자 또는 알람 내용이 비어 있습니다.")
    value = value.strip()
    if not value or len(value) > maximum or not _xml_text(value) or any(ord(c) < 32 for c in value):
        raise ValueError("장비 식별자 또는 알람 내용을 확인하세요.")
    return value


def _paired_rows(rows, cols, split, zone, devices, width):
    """Pair explicit transitions by device/category/time; multiple candidates need a row mapping."""

    def value(row, column):
        return row[column] if column is not None and column < len(row) else None

    if split is None:
        return (
            [
                (num, *(value(row, c) for c in cols), None, None, None)
                for num, row in enumerate(rows, 2)
                if any(v is not None and v != "" for v in row)
            ],
            [],
            0,
        )
    if not isinstance(split, dict) or set(split) != {
        "state",
        "category",
        "occurs",
        "recovers",
        "pairs",
    }:
        raise ValueError("발생·복구 분리 행의 열 매핑을 확인하세요.")
    status_col, key_col = split["state"], split["category"]
    if (
        any(type(c) is not int or not 0 <= c < width for c in (status_col, key_col))
        or status_col == key_col
        or cols[3] is not None
    ):
        raise ValueError("구분·이벤트 키 열을 지정하고 별도 복구 시각 열 선택을 해제하세요.")
    occurs, recovers = _string(split["occurs"], 80), _string(split["recovers"], 80)
    pairs = split["pairs"]
    if (
        occurs == recovers
        or not isinstance(pairs, dict)
        or len(pairs) > MAX_ROWS
        or any(
            not isinstance(k, str)
            or not k.isdecimal()
            or type(v) is not int
            or not 2 <= v <= len(rows) + 1
            for k, v in pairs.items()
        )
    ):
        raise ValueError("발생값·복구값과 수동 행 연결을 확인하세요.")
    errors, transitions, records = [], [], {}
    duplicate_events, seen = 0, set()
    for number, row in enumerate(rows, 2):
        if not any(v is not None and v != "" for v in row):
            continue
        try:
            device = _string(value(row, cols[0]), 80)
            matches = devices.get(device, [])
            if len(matches) != 1:
                raise ValueError("장비 키가 없거나 여러 SSH 포트에 대응합니다.")
            message = _string(value(row, cols[1]), 512)
            original = value(row, cols[2])
            timestamp = _timestamp(original, zone)
            category = _string(value(row, key_col), 160)
            state = _string(value(row, status_col), 80)
            if state not in (occurs, recovers):
                raise ValueError("등록된 발생값 또는 복구값과 다릅니다.")
            key = (matches[0], category, timestamp, state, message)
            if key in seen:
                duplicate_events += 1
                continue
            seen.add(key)
            transitions.append(
                (timestamp, state != occurs, number, matches[0], category, message, original)
            )
        except ValueError, OverflowError:
            errors.append(
                dict(row=number, reason="장비·이벤트 구분·시각·시간대 매핑을 확인하세요.")
            )
    active = {}
    blocked = set()
    for _, is_recovery, number, device, category, message, original in sorted(transitions):
        pending = active.setdefault((device, category), {})
        if not is_recovery:
            pending[number] = original
            records[number] = [number, device, message, original, None, None, None, category]
            continue
        selected = pairs.get(str(number))
        if selected is None and len(pending) == 1:
            selected = next(iter(pending))
        if selected not in pending:
            choices = list(pending)
            errors.append(
                dict(
                    row=number,
                    reason="복구와 연결할 발생 행 번호를 확인하세요.",
                    candidates=choices[:30],
                )
            )
            blocked.update(choices)
            continue
        del pending[selected]
        records[selected][4:7] = [original, number, message]
    return [row for n, row in records.items() if n not in blocked], errors, duplicate_events


def import_alarms(db, run, body, *, commit):
    name, digest, sheets, headers, rows = unpack_file(body)
    sheet = body.get("sheet") or (sheets[0] if sheets else "")
    result = dict(headers=headers, sheets=sheets, sheet=sheet, total=len(rows), digest=digest)
    mapping = body.get("columns")
    if mapping is None and not commit:
        return result
    if not isinstance(mapping, dict) or set(mapping) != set(_KEYS):
        raise ValueError("장비·알람·발생 시각 열을 지정하세요.")
    cols = [mapping[k] for k in _KEYS]
    present = [c for c in cols if c is not None]
    if (
        any(type(c) is not int or not 0 <= c < len(headers) for c in present)
        or None in cols[:3]
        or len(set(present)) != len(present)
    ):
        raise ValueError("중복 없이 유효한 열을 지정하세요.")
    zone = body.get("zone")
    polling = body.get("polling")
    if zone not in ("embedded", "UTC", "KST") or (
        polling is not None and (type(polling) is not int or not 1 <= polling <= 86400)
    ):
        raise ValueError("시간대 또는 Polling 주기(초)를 확인하세요.")
    preview_key = hashlib.sha256(
        json.dumps(
            [
                run,
                digest,
                name,
                sheet,
                body.get("encoding"),
                cols,
                zone,
                polling,
                body.get("split"),
            ],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    result["previewKey"] = preview_key
    devices = {}
    for ip, port in db.execute(
        "SELECT DISTINCT switch_ip,ssh_port FROM ping_long_series WHERE run_id=?", (run,)
    ):
        endpoint = f"{ip}:{port}"
        devices.setdefault(ip, []).append(endpoint)
        devices[endpoint] = [endpoint]
    preview, errors, valid, seen = [], [], [], set()

    prepared, split_errors, duplicates = _paired_rows(
        rows, cols, body.get("split"), zone, devices, len(headers)
    )
    errors.extend(split_errors[:20])
    invalid = len(split_errors)
    for number, device, msg, start, end, recovery_row, recovery_message, event_category in prepared:
        try:
            device = _string(device, 80)
            message = _string(msg, 512)
            matches = devices.get(device, [])
            if len(matches) != 1:
                raise ValueError("장비 키가 없거나 여러 SSH 포트에 대응합니다.")
            opened = _timestamp(start, zone)
            closed = _timestamp(end, zone) if end is not None and end != "" else None
            if closed is not None and closed < opened:
                raise ValueError("복구 시각이 발생 시각보다 빠릅니다.")
            record = (matches[0], message, opened, closed)
            key = hashlib.sha256(
                repr((*record, event_category) if event_category is not None else record).encode()
            ).hexdigest()
            if (
                key in seen
                or db.execute(
                    "SELECT 1 FROM ping_nms_events WHERE run_id=? AND dedup=?", (run, key)
                ).fetchone()
            ):
                duplicates += 1
                continue
            seen.add(key)
            valid.append(
                (
                    run,
                    key,
                    *record,
                    name,
                    sheet,
                    number,
                    str(start),
                    str(end or ""),
                    zone,
                    polling,
                    recovery_row,
                    recovery_message,
                    event_category,
                )
            )
            if len(preview) < 20:
                preview.append(
                    dict(
                        device=matches[0], message=message, opened=opened, closed=closed, row=number
                    )
                )
        except (ValueError, OverflowError) as exc:
            invalid += 1
            if len(errors) < 20:
                errors.append(
                    dict(
                        row=number,
                        reason=str(exc) if "ISO" not in str(exc) else "시각 형식을 확인하세요.",
                    )
                )
    result.update(
        preview=preview, errors=errors, valid=len(valid), invalid=invalid, duplicates=duplicates
    )
    if commit:
        if (
            body.get("digest") != digest
            or body.get("previewKey") != preview_key
            or body.get("confirm") is not True
        ):
            raise ValueError("파일 미리보기와 열 매핑을 확인하세요.")
        # Recheck only at the write boundary; parsing a 5,000-row upload must not hold a write lock.
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM ping_long_runs WHERE id=?", (run,)).fetchone():
            raise KeyError("실행을 찾을 수 없습니다.")
        before = db.total_changes
        db.executemany(
            """INSERT INTO ping_nms_events
            (run_id,dedup,device,message,opened,closed,file_name,sheet,row_number,original_start,original_end,zone,polling_seconds,recovery_row,recovery_message,event_category)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,dedup) DO NOTHING""",
            valid,
        )
        result["inserted"] = db.total_changes - before
        # Another browser may import the same approved rows while this file is parsed.
        result["duplicates"] += len(valid) - result["inserted"]
    return result


def ensure_nms(db):
    db.execute("""CREATE TABLE IF NOT EXISTS ping_nms_events(
        run_id TEXT NOT NULL REFERENCES ping_long_runs(id) ON DELETE CASCADE,
        dedup TEXT NOT NULL, device TEXT NOT NULL, message TEXT NOT NULL, opened TEXT NOT NULL,
        closed TEXT, file_name TEXT NOT NULL, sheet TEXT NOT NULL, row_number INTEGER NOT NULL,
        original_start TEXT, original_end TEXT, zone TEXT, polling_seconds INTEGER,
        imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, recovery_row INTEGER,
        recovery_message TEXT,event_category TEXT,PRIMARY KEY(run_id,dedup))""")
    extra = (("recovery_row", "INTEGER"), ("recovery_message", "TEXT"), ("event_category", "TEXT"))
    columns = {r[1] for r in db.execute("PRAGMA table_info(ping_nms_events)")}
    if any(column not in columns for column, _ in extra):
        # A different browser may be upgrading an older history at the same time.
        db.execute("BEGIN IMMEDIATE")
        columns = {r[1] for r in db.execute("PRAGMA table_info(ping_nms_events)")}
        for column, kind in extra:
            if column not in columns:
                db.execute(f"ALTER TABLE ping_nms_events ADD COLUMN {column} {kind}")
        db.commit()

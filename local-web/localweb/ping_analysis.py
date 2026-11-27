"""Local, observation-based Ping history and alarm comparison."""

import csv
import json
import math
import re
import sqlite3
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime, timedelta
from itertools import accumulate
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import Workbook

from .ping_analysis_nms import KST, ensure_nms, import_alarms

_BUCKETS = (5, 10, 30, 60, 300)
_HEAT_COLUMNS = 12
_PAGE = 100
_EXCEL_DATA_ROWS = 1_048_575


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("조회 범위 또는 페이지 번호를 확인하세요.")
    return value


def _instant(value):
    if not isinstance(value, str) or len(value) > 60:
        raise ValueError("시간대가 포함된 조회 시각을 확인하세요.")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None or not 1970 <= dt.year <= 9900:
        raise ValueError("조회 시각의 시간대를 지정하세요.")
    return dt.astimezone(UTC)


def _utc(dt):
    return dt.isoformat(timespec="milliseconds")


def _category(row):
    if row["status"] == "normal" and row["sent"] == row["received"] == 1:
        return "success"
    if row["status"] in {"no_reply", "unreachable"} and row["sent"] == 1 and row["received"] == 0:
        return "loss"
    return "gap" if row["gap_count"] or row["status"] == "not_run" else "unknown"


def _weight(row):
    return max(1, row["gap_count"] or 0)


def _percent(num, den):
    return round(100 * num / den, 2) if den else None


def _series(db, run):
    return [
        dict(row)
        for row in db.execute(
            """SELECT series_id,switch_ip,ssh_port,destination_ip,
        destination_label,source_mode,requested_source,target_count FROM ping_long_series
        WHERE run_id=? ORDER BY series_id""",
            (run,),
        )
    ]


def _samples(db, run, sid):
    return db.execute(
        "SELECT * FROM ping_long_samples WHERE run_id=? AND series_id=? ORDER BY id", (run, sid)
    )


def _events(rows):
    event, last_good = None, None
    for offset, row in enumerate(rows):
        category = _category(row)
        window = [row["started_at"], row["received_at"] or row["finished_at"]]
        if category == "loss":
            if event is None:
                event = dict(
                    lastGood=last_good,
                    first=window,
                    last=window,
                    count=0,
                    firstId=row["id"],
                    sampleOffset=(offset // _PAGE) * _PAGE,
                    observedSeconds=None,
                    recovery=None,
                    clockUncertain=False,
                )
            event["count"] += 1
            event["last"] = window
            event["clockUncertain"] |= bool(row["clock_uncertain"])
            continue
        if event:
            event["state"] = "observed_recovery" if category == "success" else "interrupted"
            event["recovery"] = window if category == "success" else None
            event["clockUncertain"] |= bool(row["clock_uncertain"])
            if (
                event["recovery"]
                and not event["clockUncertain"]
                and event["first"][1]
                and window[1]
            ):
                elapsed = (_instant(window[1]) - _instant(event["first"][1])).total_seconds()
                event["observedSeconds"] = elapsed if elapsed >= 0 else None
            yield event
            event = None
        last_good = window if category == "success" and not row["clock_uncertain"] else None
    if event:
        event["state"] = "open"
        yield event


def _metrics(rows, target_count):
    counts, rtts = Counter(), []
    attempts = longest = streak = 0
    for row in rows:
        cat = _category(row)
        counts[cat] += _weight(row)
        attempts += row["attempt"] is not None
        streak = streak + 1 if cat == "loss" else 0
        longest = max(longest, streak)
        if cat == "success" and row["rtt_ms"] is not None:
            rtts.append(row["rtt_ms"])
    rtts.sort()
    n = len(rtts)
    valid = counts["success"] + counts["loss"]
    return dict(
        success=counts["success"],
        loss=counts["loss"],
        unknown=counts["unknown"],
        gap=counts["gap"],
        attempts=attempts,
        target=target_count,
        coverage=_percent(valid, sum(counts.values())),
        goalCoverage=_percent(valid, target_count),
        lossRate=_percent(counts["loss"], valid),
        streak=streak,
        longest=longest,
        rttCount=n,
        minimum=rtts[0] if n else None,
        maximum=rtts[-1] if n else None,
        mean=round(sum(rtts) / n, 3) if n else None,
        p95=rtts[math.ceil(n * 0.95) - 1] if n else None,
        p99=rtts[math.ceil(n * 0.99) - 1] if n else None,
    )


def _series_report(db, run, meta, body):
    offset = _integer(body.get("offset", 0), 0, 2**31 - 1)
    event_offset = _integer(body.get("eventOffset", 0), 0, 2**31 - 1)
    start = _instant(body["start"]) if body.get("start") else None
    end = _instant(body["end"]) if body.get("end") else None
    if start and end and end < start:
        raise ValueError("조회 종료 시각을 확인하세요.")
    # Per series bounded by the engine's per-combination attempt cap. Aggregate gaps in storage.
    query = "SELECT * FROM ping_long_samples WHERE run_id=? AND series_id=?"
    params = [run, meta["series_id"]]
    if start:
        query += " AND coalesce(received_at,finished_at,scheduled_at)>=?"
        params.append(_utc(start))
    if end:
        query += " AND coalesce(received_at,finished_at,scheduled_at)<=?"
        params.append(_utc(end))
    rows = db.execute(query + " ORDER BY id", params).fetchall()
    metrics = _metrics(rows, meta["target_count"])
    events = list(_events(rows))
    points = []
    timed = [
        (r, _instant(r["received_at"] or r["finished_at"] or r["scheduled_at"]).timestamp())
        for r in rows
    ]
    if timed:
        earliest = min(t for _, t in timed)
        latest = max(t for _, t in timed)
        width = max(1, math.ceil((latest - earliest + 1) / 120))
        bins = {}
        for r, t in timed:
            key = int((t - earliest) // width)
            bucket = bins.setdefault(
                key,
                dict(
                    utc=_utc(datetime.fromtimestamp(earliest + key * width, UTC)),
                    success=0,
                    loss=0,
                    gap=0,
                    unknown=0,
                    maxRtt=None,
                ),
            )
            category = _category(r)
            bucket[category] += _weight(r)
            if category == "success" and r["rtt_ms"] is not None:
                bucket["maxRtt"] = max(bucket["maxRtt"] or 0, r["rtt_ms"])
        points = [
            bins.get(
                k,
                dict(
                    utc=_utc(datetime.fromtimestamp(earliest + k * width, UTC)),
                    success=0,
                    loss=0,
                    gap=0,
                    unknown=0,
                    maxRtt=None,
                ),
            )
            for k in range(max(bins) + 1)
        ]
    return dict(
        series=meta,
        metrics=metrics,
        eventCount=len(events),
        events=events[event_offset : event_offset + _PAGE],
        samples=[dict(row) for row in rows[offset : offset + _PAGE]],
        sampleCount=len(rows),
        points=points,
    )


def _heat_cell(rows):
    counts = Counter()
    for r in rows:
        counts[_category(r)] += _weight(r)
    valid = counts["loss"] + counts["success"]
    return dict(
        success=counts["success"],
        loss=counts["loss"],
        unknown=counts["unknown"],
        gap=counts["gap"],
        rate=_percent(counts["loss"], valid),
        coverage=_percent(valid, sum(counts.values())),
    )


def _overlap(cells, left, right):
    observed = []
    for device, rows in cells.items():
        for row in rows:
            category = _category(row)
            begin, end = row["started_at"], row["received_at"] or row["finished_at"]
            if (
                category in ("success", "loss")
                and begin
                and end
                and begin <= end
                and not row["clock_uncertain"]
            ):
                observed.append((max(begin, left), min(end, right), device, category))
    active = []
    overlap, healthy = set(), set()
    for begin, end, device, category in sorted(observed):
        if begin > end:
            continue
        active = [item for item in active if item[0] >= begin]
        current = [*active, (end, device, category)]
        failed = {dev for _, dev, cat in current if cat == "loss"}
        if len(failed) >= 2:
            overlap.update(failed)
            healthy.update(dev for _, dev, cat in current if cat == "success")
        active = current
    return sorted(overlap), sorted(healthy)


def _heat(db, run, series, body):
    dest = body.get("destination")
    members = [m for m in series if m["destination_ip"] == dest]
    if not members:
        raise ValueError("실행에 포함된 목적지를 선택하세요.")
    seconds = body.get("bucket", 60)
    if type(seconds) is not int or seconds not in _BUCKETS:
        raise ValueError("시간 버킷을 확인하세요.")
    first = _instant(body.get("start"))
    times = [first + timedelta(seconds=i * seconds) for i in range(_HEAT_COLUMNS + 1)]
    left, right = _utc(times[0]), _utc(times[-1])
    devices = {m["series_id"]: f"{m['switch_ip']}:{m['ssh_port']}" for m in members}
    buckets = [defaultdict(list) for _ in times[:-1]]
    # A clock jump disqualifies concurrency evidence; the receipt bucket still shows coverage.
    query = """SELECT s.* FROM ping_long_samples s JOIN ping_long_series m USING(run_id,series_id)
        WHERE s.run_id=? AND m.destination_ip=? AND coalesce(s.received_at,s.finished_at,s.scheduled_at)>=?
        AND coalesce(s.started_at,s.received_at,s.finished_at,s.scheduled_at)<? ORDER BY s.id"""
    for row in db.execute(query, (run, dest, left, right)):
        finish = _instant(row["received_at"] or row["finished_at"] or row["scheduled_at"])
        start = (
            _instant(row["started_at"])
            if row["started_at"] and not row["clock_uncertain"]
            else finish
        )
        low = max(0, math.floor((min(start, finish) - first).total_seconds() / seconds))
        high = min(len(buckets) - 1, math.floor((finish - first).total_seconds() / seconds))
        for i in range(low, high + 1):
            buckets[i][devices[row["series_id"]]].append(row)
    groups = _groups(db, run, series)
    overlap = []
    for i, by_device in enumerate(buckets):
        failures, counter = _overlap(by_device, _utc(times[i]), _utc(times[i + 1]))
        overdue = [device for device, rows in by_device.items() if _heat_cell(rows)["loss"]]
        overlap.append(
            dict(
                devices=failures,
                counter=counter,
                failureGroups=sorted({groups[d] for d in failures if d in groups}),
                healthyGroups=sorted({groups[d] for d in counter if d in groups}),
                groupedFailureCount=sum(d in groups for d in failures),
                failedCount=len(overdue),
                observed=sum(
                    any(_category(r) in ("loss", "success") for r in rows)
                    for rows in by_device.values()
                ),
            )
        )
    return dict(
        times=[_utc(t) for t in times[:-1]],
        bucket=seconds,
        overlap=overlap,
        rows=[
            dict(
                device=dev,
                group=groups.get(dev),
                cells=[_heat_cell(b.get(dev, [])) for b in buckets],
            )
            for dev in devices.values()
        ],
    )


def _alarm_index(db, run, endpoints):
    """Count interval overlaps without rescanning a long series for each imported alarm."""
    streams = defaultdict(lambda: defaultdict(lambda: ([], [])))
    for device, destination, sid in endpoints:
        groups = streams[(device, destination)]
        for row in _samples(db, run, sid):
            begin = row["started_at"] or row["scheduled_at"]
            end = row["received_at"] or row["finished_at"] or begin
            category = _category(row)
            if (
                row["clock_uncertain"]
                or not row["started_at"]
                or begin > end
                or category not in ("success", "loss")
            ):
                category = "unknown"
            weight = _weight(row)
            starts, ends = groups[category]
            starts.append((min(begin, end), weight))
            ends.append((max(begin, end), weight))

    def cumulative(entries):
        entries.sort()
        return ([t for t, _ in entries], [0, *accumulate(w for _, w in entries)])

    return {
        key: {kind: (cumulative(begin), cumulative(end)) for kind, (begin, end) in group.items()}
        for key, group in streams.items()
    }


def _bounds(index, alarm, tolerance, now):
    opened = _instant(alarm["opened"])
    closed = _instant(alarm["closed"]) if alarm["closed"] else now
    left, right = _utc(opened), _utc(closed)

    def count(kind, start, end):
        if kind not in index or end < start:
            return 0
        (starts, sums), (ends, expired) = index[kind]
        return sums[bisect_right(starts, end)] - expired[bisect_left(ends, start)]

    exact = count("loss", left, right)
    return dict(
        strictLoss=exact,
        strictSuccess=count("success", left, right),
        nearLoss=count(
            "loss",
            _utc(opened - timedelta(seconds=tolerance)),
            _utc(closed + timedelta(seconds=tolerance)),
        )
        - exact,
        unknown=count("unknown", left, right),
    )


def _nms_compare(db, run, series, body):
    dest = body.get("destination")
    if not any(m["destination_ip"] == dest for m in series):
        raise ValueError("비교할 NMS 목적지를 선택하세요.")
    offset = _integer(body.get("offset", 0), 0, 2**31 - 1)
    tolerance = _integer(body.get("tolerance", 60), 0, 3600)
    events = db.execute(
        """SELECT * FROM ping_nms_events WHERE run_id=? ORDER BY opened DESC,dedup
        LIMIT 51 OFFSET ?""",
        (run, offset),
    ).fetchall()
    devices = {r["device"] for r in events[:50]}
    endpoints = [
        (f"{m['switch_ip']}:{m['ssh_port']}", dest, m["series_id"])
        for m in series
        if m["destination_ip"] == dest and f"{m['switch_ip']}:{m['ssh_port']}" in devices
    ]
    index = _alarm_index(db, run, endpoints)
    now = datetime.now(UTC)
    return dict(
        rows=[
            {**dict(a), **_bounds(index.get((a["device"], dest), {}), a, tolerance, now)}
            for a in events[:50]
        ],
        more=len(events) > 50,
    )


def _alarm_export(db, run, series):
    yield [
        "장비",
        "비교 목적지",
        "알람",
        "발생 UTC",
        "복구 UTC",
        "Polling 초",
        "겹친 손실",
        "겹친 정상",
        "인접 손실 (±60초)",
        "미확인·미측정",
        "원본 파일",
        "시트",
        "행",
    ]
    alarms = db.execute("SELECT * FROM ping_nms_events WHERE run_id=? ORDER BY opened", (run,))
    devices = {
        row[0]
        for row in db.execute("SELECT DISTINCT device FROM ping_nms_events WHERE run_id=?", (run,))
    }
    endpoints = [
        (f"{m['switch_ip']}:{m['ssh_port']}", m["destination_ip"], m["series_id"])
        for m in series
        if f"{m['switch_ip']}:{m['ssh_port']}" in devices
    ]
    index = _alarm_index(db, run, endpoints)
    destinations = defaultdict(list)
    for device, dest, _ in endpoints:
        destinations[device].append(dest)
    now = datetime.now(UTC)
    for alarm in alarms:
        device = alarm["device"]
        for dest in destinations[device]:
            counts = _bounds(index.get((device, dest), {}), alarm, 60, now)
            yield [
                device,
                dest,
                alarm["message"],
                alarm["opened"],
                alarm["closed"],
                alarm["polling_seconds"],
                counts["strictLoss"],
                counts["strictSuccess"],
                counts["nearLoss"],
                counts["unknown"],
                alarm["file_name"],
                alarm["sheet"],
                alarm["row_number"],
            ]


@contextmanager
def _connection(path):
    if not Path(path).is_file():
        raise KeyError("이력이 없습니다.")
    with closing(sqlite3.connect(path, timeout=4)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        with db:
            yield db


def _run(db, run):
    if not isinstance(run, str) or not re.fullmatch(r"[a-f0-9]{32}", run):
        raise ValueError("장시간 Ping 실행을 선택하세요.")
    row = db.execute(
        "SELECT id,started_at,finished_at,state,reason FROM ping_long_runs WHERE id=?", (run,)
    ).fetchone()
    if not row:
        raise KeyError("실행을 찾을 수 없습니다.")
    return dict(row)


def _groups(db, run, series, mapping=None):
    db.execute("""CREATE TABLE IF NOT EXISTS ping_analysis_groups(
        run_id TEXT NOT NULL REFERENCES ping_long_runs(id) ON DELETE CASCADE,
        device TEXT NOT NULL, group_name TEXT NOT NULL, PRIMARY KEY(run_id,device))""")
    if mapping is not None:
        valid = {f"{m['switch_ip']}:{m['ssh_port']}" for m in series}
        if (
            not isinstance(mapping, dict)
            or not set(mapping) <= valid
            or any(
                not isinstance(label, str)
                or not 1 <= len(label.strip()) <= 64
                or any(ord(c) < 32 for c in label)
                for label in mapping.values()
            )
        ):
            raise ValueError("대상 스위치의 IP:SSH포트와 그룹 이름을 확인하세요.")
        db.execute("DELETE FROM ping_analysis_groups WHERE run_id=?", (run,))
        db.executemany(
            "INSERT INTO ping_analysis_groups VALUES(?,?,?)",
            [(run, device, label.strip()) for device, label in mapping.items()],
        )
    return dict(
        db.execute(
            "SELECT device,group_name FROM ping_analysis_groups WHERE run_id=? ORDER BY device",
            (run,),
        )
    )


def _history_management(jobs, action, body):
    if jobs is None:
        raise ValueError("이력 관리 연결이 필요합니다.")
    with jobs.lock:
        manager = jobs.long_ping
        store = manager.history
        if action == "policy":
            return store.policy()
        if body.get("confirm") is not True:
            raise ValueError("이력 변경을 확인하세요.")
        if action == "policy-save":
            store.set_retention(
                _integer(body.get("days"), 1, 365), _integer(body.get("maxMb"), 32, 2048)
            )
            return store.policy()
        run = body.get("run")
        with store.connect() as db:
            _run(db, run)
            if not store.delete_completed(db, run):
                raise ValueError("종료된 실행만 삭제할 수 있습니다.")
        if manager.run and manager.run.key == run:
            manager.run = None
        return {"deleted": True}


def analysis_request(path, action, body, *, jobs=None):
    if not isinstance(body, dict):
        raise ValueError("분석 요청 형식을 확인하세요.")
    if action in ("policy", "policy-save", "delete"):
        return _history_management(jobs, action, body)
    if action == "runs" and not Path(path).is_file():
        return {"runs": []}
    with _connection(path) as db:
        if action == "runs":
            offset = _integer(body.get("offset", 0), 0, 2**31 - 1)
            rs = db.execute(
                "SELECT id,started_at,finished_at,state FROM ping_long_runs ORDER BY started_at DESC,id LIMIT 101 OFFSET ?",
                (offset,),
            ).fetchall()
            return {"runs": [dict(r) for r in rs[:100]], "more": len(rs) > 100}
        run = body.get("run")
        meta = _run(db, run)
        series = _series(db, run)
        if action == "series":
            return {"run": meta, "series": series, "groups": _groups(db, run, series)}
        if action == "groups":
            if not isinstance(body.get("groups"), dict):
                raise ValueError("스위치 그룹을 확인하세요.")
            return {"groups": _groups(db, run, series, body["groups"])}
        if action == "report":
            sid = _integer(body.get("series"), 0, 2**31 - 1)
            for item in series:
                if item["series_id"] == sid:
                    return _series_report(db, run, item, body)
            raise ValueError("실행에 속한 조합을 선택하세요.")
        if action == "heat":
            return _heat(db, run, series, body)
        if action == "path":
            return _path_points(db, run, series, body)
        if action in ("nms-file", "nms-commit", "nms-compare"):
            ensure_nms(db)
            if action == "nms-compare":
                return _nms_compare(db, run, series, body)
            return import_alarms(db, run, body, commit=action == "nms-commit")
    raise ValueError("지원하지 않는 분석 요청입니다.")


def _path_points(db, run, series, body):
    ids = body.get("series")
    if (
        not isinstance(ids, list)
        or len(ids) != 3
        or any(type(s) is not int for s in ids)
        or len(set(ids)) != 3
    ):
        raise ValueError("백본·중간 L3·서버에 서로 다른 측정 조합을 지정하세요.")
    members = {m["series_id"]: m for m in series}
    if (
        any(s not in members for s in ids)
        or len(
            {
                (
                    members[s]["switch_ip"],
                    members[s]["ssh_port"],
                    members[s]["source_mode"],
                    members[s]["requested_source"],
                )
                for s in ids
            }
        )
        != 1
    ):
        raise ValueError("같은 스위치와 Source를 사용한 관측 지점을 선택하세요.")
    start = _instant(body.get("start"))
    seconds = _integer(body.get("seconds", 60), 5, 300)
    left, right = _utc(start), _utc(start + timedelta(seconds=seconds))
    states, counts = [], []
    verified_sources = set()
    for sid in ids:
        rows = db.execute(
            """SELECT * FROM ping_long_samples WHERE run_id=? AND series_id=? AND started_at>=?
            AND coalesce(received_at,finished_at)<=? AND clock_uncertain=0""",
            (run, sid, left, right),
        ).fetchall()
        count = _heat_cell(rows)
        verified_sources.update(
            r["verified_source_ip"] for r in rows if _category(r) in {"success", "loss"}
        )
        counts.append(count)
        states.append(
            "success"
            if count["success"] and not count["loss"]
            else "loss"
            if count["loss"] and not count["success"]
            else "inconclusive"
        )
    candidates = {
        ("success", "success", "loss"): "목적지 측 경로·서버 ICMP 정책을 확인하세요.",
        ("success", "loss", "loss"): "중간 L3 이후의 공통 경로·정책을 확인하세요.",
        ("loss", "loss", "loss"): "스위치 관리망과 초입 공통 경로를 확인하세요.",
        ("loss", "success", "success"): "백본 자체 ICMP 응답 제한 가능성이 있습니다.",
    }
    same_source = len(verified_sources) == 1 and None not in verified_sources
    candidate = (
        candidates.get(tuple(states))
        if body.get("baselineVerified") is True and same_source
        else None
    )
    return dict(
        states=states,
        counts=counts,
        candidate=candidate,
        fromUtc=left,
        toUtc=right,
        sourceVerified=same_source,
    )


def _safe(value):
    if isinstance(value, str):
        # Encode unrepresentable XML controls visibly, including legacy/imported labels.
        value = re.sub(
            r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]",
            lambda m: f"\\u{ord(m[0]):04x}",
            value,
        )
        if value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


def _kst(value):
    return _instant(value).astimezone(KST).isoformat(timespec="milliseconds") if value else None


def _sample_export(db, run):
    rows = db.execute(
        """SELECT m.switch_ip,m.ssh_port,m.destination_ip,m.destination_label,
        m.source_mode,m.requested_source,s.id,s.run_id,s.series_id,s.scheduled_at,s.started_at,
        s.received_at,s.finished_at,s.attempt,s.status,s.sent,s.received,s.rtt_ms,s.rtt_evidence,s.reason,
        s.gap_count,s.clock_uncertain,s.duration_ms,s.verified_source_ip FROM ping_long_samples s JOIN ping_long_series m
        USING(run_id,series_id) WHERE s.run_id=? ORDER BY s.series_id,s.id""",
        (run,),
    )
    keys = [d[0] for d in rows.description]
    times = ("scheduled_at", "started_at", "received_at", "finished_at")
    yield [*keys, "observed_kst", *(key + "_kst" for key in times)]
    for row in rows:
        converted = {value: _kst(value) for value in {row[key] for key in times}}
        yield [
            *tuple(row),
            converted[row["received_at"] or row["finished_at"]],
            *(converted[row[key]] for key in times),
        ]


def _sheet(book, name, rows):
    iterator = iter(rows)
    head = next(iterator)
    ws = book.create_sheet(name)
    ws.append(head)
    for i, row in enumerate(iterator):
        if i and i % _EXCEL_DATA_ROWS == 0:
            ws = book.create_sheet(f"{name} {1 + i // _EXCEL_DATA_ROWS}")
            ws.append(head)
        ws.append([_safe(v) for v in row])


def _summary_export(db, run, series):
    yield [
        "장비",
        "SSH 포트",
        "목적지",
        "Source",
        "목표",
        "시도",
        "성공",
        "확인 손실",
        "미확인",
        "미측정",
        "유효 손실률 (%)",
        "관측 커버리지 (%)",
        "정확 RTT 표본 수",
        "평균 ms",
        "P95 ms",
        "P99 ms",
    ]
    for m in series:
        metrics = _metrics(_samples(db, run, m["series_id"]), m["target_count"])
        yield [
            m["switch_ip"],
            m["ssh_port"],
            m["destination_ip"],
            m["requested_source"],
            *(
                metrics[k]
                for k in (
                    "target",
                    "attempts",
                    "success",
                    "loss",
                    "unknown",
                    "gap",
                    "lossRate",
                    "coverage",
                    "rttCount",
                    "mean",
                    "p95",
                    "p99",
                )
            ),
        ]


def _event_export(db, run, series):
    yield [
        "장비",
        "SSH 포트",
        "목적지",
        "마지막 정상 UTC",
        "첫 손실 시작 UTC",
        "첫 손실 수신 UTC",
        "마지막 손실 수신 UTC",
        "복구 수신 UTC",
        "연속 손실",
        "상태",
        "시계 불확실",
    ]
    for m in series:
        for e in _events(_samples(db, run, m["series_id"])):
            yield [
                m["switch_ip"],
                m["ssh_port"],
                m["destination_ip"],
                (e["lastGood"] or [None, None])[1],
                *e["first"],
                e["last"][1],
                (e["recovery"] or [None, None])[1],
                e["count"],
                e["state"],
                e["clockUncertain"],
            ]


def _correlation_export(db, run):
    yield [
        "목적지",
        "관측 시각 UTC",
        "겹친 손실 장비",
        "같은 시간창 정상",
        "손실 장비 수",
        "확인 범위",
    ]
    rows = db.execute(
        """SELECT s.started_at,coalesce(s.received_at,s.finished_at) AS observed,
        s.status,s.sent,s.received,s.gap_count,m.switch_ip,m.ssh_port,m.destination_ip
        FROM ping_long_samples s JOIN ping_long_series m USING(run_id,series_id)
        WHERE s.run_id=? AND s.started_at IS NOT NULL AND s.clock_uncertain=0
        AND coalesce(s.received_at,s.finished_at)>=s.started_at ORDER BY m.destination_ip,s.started_at,s.id""",
        (run,),
    )
    active = []
    last_destination = None
    for row in rows:
        destination = row["destination_ip"]
        if destination != last_destination:
            active = []
            last_destination = destination
        category = _category(row)
        if category not in ("success", "loss"):
            continue
        now = row["started_at"]
        active = [item for item in active if item[0] >= now]
        active.append((row["observed"], f"{row['switch_ip']}:{row['ssh_port']}", category))
        failed = sorted({d for _, d, c in active if c == "loss"})
        if len(failed) >= 2:
            yield [
                destination,
                now,
                ", ".join(failed),
                ", ".join(sorted({d for _, d, c in active if c == "success"})),
                len(failed),
                "명령 관측 시간창 겹침 · 실제 원인 미확정",
            ]


def _path_export(history_path, run):
    yield [
        "장비",
        "목적지",
        "확인 Source",
        "구분",
        "진단 시작 UTC",
        "장애 관측 UTC",
        "시간차 초",
        "상태",
        "TTL",
        "Hop",
        "RTT ms",
    ]
    route_path = Path(history_path).with_name("web-history.sqlite")
    if not route_path.is_file():
        return
    with _connection(route_path) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ping_routes'"
        ).fetchone():
            return
        # Automatic route observations carry this run's event ID. Manual observations have no such linkage.
        for row in db.execute(
            "SELECT output FROM ping_routes WHERE event_id LIKE ? ORDER BY id", (run + ":%",)
        ):
            r = json.loads(row[0])
            for hop in r.get("hops") or [{}]:
                yield [
                    r.get("endpoint"),
                    r.get("destination"),
                    r.get("sourceIp"),
                    r.get("purpose"),
                    r.get("observedAt"),
                    r.get("failedAt"),
                    r.get("delaySeconds"),
                    r.get("state"),
                    hop.get("ttl"),
                    hop.get("ip"),
                    hop.get("rttMs"),
                ]


@contextmanager
def _export_book():
    book = Workbook(write_only=True)
    try:
        yield book
    finally:
        # The pinned OpenPyXL version otherwise keeps incomplete worksheet XML until process exit.
        for ws in book.worksheets:
            writer = ws._writer
            if writer and Path(writer.out).is_file():
                with suppress(Exception):
                    ws.close()
                with suppress(OSError, ValueError):
                    writer.cleanup()
        book.close()


@contextmanager
def export_history(path, run, extension):
    if extension not in ("csv", "xlsx"):
        raise ValueError("내보낼 형식을 확인하세요.")
    with TemporaryDirectory(prefix="ping-export-") as folder:
        file = Path(folder) / f"Ping-{run}.{extension}"
        with _connection(path) as db:
            # Run existence and every measurement worksheet belong to the same snapshot.
            db.execute("BEGIN")
            _run(db, run)
            if extension == "csv":
                with file.open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerows([_safe(v) for v in row] for row in _sample_export(db, run))
            else:
                with _export_book() as book:
                    series = _series(db, run)
                    _sheet(book, "요약", _summary_export(db, run, series))
                    _sheet(book, "개별 Ping", _sample_export(db, run))
                    _sheet(book, "장애·복구 이벤트", _event_export(db, run, series))
                    _sheet(book, "동시 장애 관측", _correlation_export(db, run))
                    _sheet(book, "경로 진단", _path_export(path, run))
                    if db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ping_nms_events'"
                    ).fetchone():
                        _sheet(book, "NMS 비교", _alarm_export(db, run, series))
                        rows = db.execute(
                            "SELECT * FROM ping_nms_events WHERE run_id=? ORDER BY opened", (run,)
                        )

                        def alarm_rows():
                            yield [*(d[0] for d in rows.description), "opened_kst", "closed_kst"]
                            for alarm in rows:
                                yield [*alarm, _kst(alarm["opened"]), _kst(alarm["closed"])]

                        _sheet(book, "NMS 이력", alarm_rows())
                    book.save(file)
        yield file

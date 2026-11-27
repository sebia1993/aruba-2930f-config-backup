"""Bounded, model-checked route observation on the shared device worker."""

import json
import re
import time
from collections import deque
from datetime import UTC, datetime

from aruba2930f_backup.models import CollectionFailure, CollectionOptions, ErrorCode
from aruba2930f_backup.ssh import _mapped_failure, _strip_command_and_prompt
from aruba2930f_backup.validation import validate_cli_response

from .ping_long_store import history_connection
from .ping_source import parse_owned_ipv4
from .ping_ssh import PingSSHSession, _check_cancel
from .ping_tools import PingPlan, unicast_ipv4, verify_source

MAX_HOPS = 12
HOP = re.compile(r"^\s*(\d{1,2})\s+(.*?)\s*$")
PROBE = re.compile(
    r"(\d{1,3}(?:\.\d{1,3}){3})\s+(\d+(?:\.\d+)?)\s*(ms|msec|us|usec|µs|μs|s|sec)", re.I
)
PURPOSES = {"baseline", "incident", "recovered", "event"}


def parse_hops(output, destination):
    """Only accept consecutive TTLs from a single-probe response; retain explicit units."""
    hops = []
    for line in output.splitlines():
        match = HOP.fullmatch(line)
        if not match:
            if line.strip() and not line.lower().strip().startswith(("traceroute ", "press ")):
                return [], "unknown"
            continue
        ttl = int(match[1])
        if ttl != len(hops) + 1 or ttl > MAX_HOPS:
            return [], "unknown"
        if match[2] == "*":
            hops.append({"ttl": ttl, "ip": None, "rttMs": None})
            continue
        probe = PROBE.fullmatch(match[2])
        if not probe:
            return [], "unknown"
        try:
            address = unicast_ipv4(probe[1])
        except ValueError:
            return [], "unknown"
        unit = probe[3].casefold()
        factor = 1000 if unit in {"s", "sec"} else 1 if unit in {"ms", "msec"} else 0.001
        hops.append({"ttl": ttl, "ip": address, "rttMs": round(float(probe[2]) * factor, 6)})
    if any(h["ip"] == destination for h in hops[:-1]):
        return [], "unknown"
    if hops and hops[-1]["ip"] == destination:
        return hops, "reached"
    return hops, "partial" if hops else "unknown"


class RouteSession(PingSSHSession):
    def observe(self, plan, cancel):
        self._require_prepared()
        identity = self._identify(cancel)
        target = plan.targets[0]
        dest = plan.destinations[0].ip
        source = plan.requested_source(target)
        verified = None
        command = f"traceroute {dest} maxttl {MAX_HOPS} probes 1"
        if source is not None:
            owned = parse_owned_ipv4(
                self._source_table(vlan=True, cancel=cancel),
                self._source_table(vlan=False, cancel=cancel),
                completed=True,
            )
            verify_source(plan, target, owned)
            # Use the confirmed interface address in the documented IPv4 source form.
            verified = (
                next(ip for vid, ip in owned if vid == plan.options.vlan)
                if plan.options.source_mode == "vlan"
                else source
            )
            command += f" source {verified}"
        command += " timeout 1"
        conn = self._require_connection()
        prompt = self._require_prepared()
        _check_cancel(cancel)
        conn.clear_buffer()
        _check_cancel(cancel)
        try:
            conn.write_channel(conn.normalize_cmd(command))
            raw = self._read_until_prompt(prompt, cancel_event=cancel, command=command)
        except Exception as exc:
            raise _mapped_failure(exc, during="command") from exc
        output = _strip_command_and_prompt(raw, command, prompt)
        validate_cli_response(command, output)
        hops, state = parse_hops(output, dest)
        return {
            "hops": hops,
            "state": state,
            "sourceIp": verified,
            "model": identity.model,
            "version": identity.software_version,
        }


def run_route(operations, data, cancel):
    if (
        set(data) - {"plan", "purpose", "eventId", "failedAt"}
        or data.get("purpose") not in PURPOSES
    ):
        raise ValueError("경로 진단 유형을 확인하세요.")
    plan = PingPlan.parse(data.get("plan"))
    if plan.total_pairs != 1:
        raise ValueError("경로 진단은 스위치 한 대와 목적지 한 곳을 선택하세요.")
    purpose = data["purpose"]
    event = data.get("eventId")
    failed = data.get("failedAt")
    history_path = operations.data_dir / "ping-history.sqlite"
    run_id = None
    if purpose == "event":
        if not isinstance(event, str) or not re.fullmatch(
            r"[0-9a-f]{32}:[0-9]{1,10}:[0-9]{1,5}", event
        ):
            raise ValueError("측정 이벤트를 확인하세요.")
        if (
            not isinstance(failed, str)
            or len(failed) > 40
            or datetime.fromisoformat(failed).tzinfo is None
        ):
            raise ValueError("장애 관측 시각을 확인하세요.")
        run_id, series_id, first_attempt = event.split(":")
        with history_connection(history_path) as db:
            if not db.execute("SELECT 1 FROM ping_long_runs WHERE id=?", (run_id,)).fetchone():
                return {"state": "discarded", "reason": "RUN_DELETED", "hops": []}
    elif event is not None or failed is not None:
        raise ValueError("수동 경로 진단 요청을 확인하세요.")
    options = CollectionOptions(max_attempts=1, command_timeout_seconds=32, max_output_bytes=65536)
    target = plan.targets[0]
    dest = plan.destinations[0].ip
    started = datetime.now(UTC).isoformat(timespec="milliseconds")
    try:
        with operations._prepared_diagnostic_session(
            RouteSession, target, operations.credentials, options, cancel
        ) as session:
            result = session.observe(plan, cancel)
    except CollectionFailure as exc:
        state = {
            ErrorCode.CANCELLED: "cancelled",
            ErrorCode.MODEL_UNSUPPORTED: "unsupported",
            ErrorCode.COMMAND_REJECTED: "cli_error",
            ErrorCode.COMMAND_TIMEOUT: "unknown",
            ErrorCode.PROMPT_PARSE_FAILED: "unknown",
        }.get(exc.code, "ssh_failure")
        result = {"state": state, "reason": exc.code.value, "hops": [], "sourceIp": None}
    except ValueError:
        result = {
            "state": "source_error",
            "reason": "SOURCE_UNVERIFIED",
            "hops": [],
            "sourceIp": None,
        }
    result.update(
        endpoint=target.endpoint,
        destination=dest,
        observedAt=started,
        purpose=purpose,
        finishedAt=datetime.now(UTC).isoformat(timespec="milliseconds"),
        eventId=event,
        failedAt=failed,
    )
    if failed:
        result["delaySeconds"] = (
            datetime.fromisoformat(started) - datetime.fromisoformat(failed)
        ).total_seconds()
    source_key = f"{plan.options.source_mode}:{result['sourceIp'] or ''}:{plan.requested_source(target) or ''}"
    # Check ownership and publish under the same writer transaction as deletion.
    # A late result must not recreate an observation after its run was removed.
    with history_connection(history_path) as db:
        db.execute("BEGIN IMMEDIATE")
        if run_id is not None:
            if not db.execute("SELECT 1 FROM ping_long_runs WHERE id=?", (run_id,)).fetchone():
                return {"state": "discarded", "reason": "RUN_DELETED", "hops": []}
            recovered = db.execute(
                """SELECT 1 FROM ping_long_samples
                   WHERE run_id=? AND series_id=? AND attempt>? AND status='normal'
                   AND sent=1 AND received=1 AND finished_at<=? LIMIT 1""",
                (run_id, int(series_id), int(first_attempt), started),
            ).fetchone()
            result["recoveredBeforeTrace"] = bool(recovered)
        previous = db.execute(
            "SELECT observed,output FROM ping_routes WHERE endpoint=? AND destination=? AND source_key=? AND purpose='baseline' ORDER BY id DESC LIMIT 1",
            (target.endpoint, dest, source_key),
        ).fetchone()
        result["baselineAt"] = previous[0] if previous and result["sourceIp"] else None
        baseline = json.loads(previous[1])["hops"] if result["baselineAt"] else []
        result["baselineHops"] = baseline
        result["changedTtls"] = [
            h["ttl"]
            for old, h in zip(baseline, result["hops"], strict=False)
            if old["ip"] and h["ip"] and old["ip"] != h["ip"]
        ]
        if purpose == "baseline" and (result["state"] != "reached" or not result["sourceIp"]):
            # Retain the attempted observation without replacing a healthy baseline.
            purpose = result["purpose"] = "baseline_unconfirmed"
        db.execute(
            "INSERT INTO ping_routes(run_id,endpoint,destination,source_key,observed,purpose,event_id,failed_at,output) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                target.endpoint,
                dest,
                source_key,
                started,
                purpose,
                event,
                failed,
                json.dumps(result, ensure_ascii=False),
            ),
        )
        db.execute(
            """DELETE FROM ping_routes WHERE id IN
                (SELECT id FROM ping_routes WHERE (run_id IS NULL)=? ORDER BY id DESC LIMIT -1 OFFSET 2000)""",
            (int(run_id is None),),
        )
    return result


def recent_routes(jobs):
    with jobs.long_ping.history.connect() as db:
        return {
            "items": [
                json.loads(row[0])
                for row in db.execute("SELECT output FROM ping_routes ORDER BY id DESC LIMIT 50")
            ]
        }


class TraceQueue:
    """Automatic diagnostics yield to existing jobs and are never replayed on restart."""

    def __init__(self):
        self.pending = deque()
        self.cooldown = {}

    def offer(self, key, plan, target, dest, event, failed, credentials):
        now = time.monotonic()
        self.cooldown = {k: v for k, v in self.cooldown.items() if v > now}
        pair = (target.endpoint, dest.ip, plan.requested_source(target))
        if pair in self.cooldown or len(self.pending) >= 8:
            return
        self.cooldown[pair] = now + 300
        options = {"source_mode": plan.options.source_mode}
        source_ips = {}
        if plan.options.source_mode == "vlan":
            options["vlan"] = plan.options.vlan
        if plan.options.source_mode == "ip":
            source_ips[target.endpoint] = plan.requested_source(target)
        body = {
            "purpose": "event",
            "eventId": event,
            "failedAt": failed,
            "plan": {
                "targets": target.endpoint,
                "destinations": [{"ip": dest.ip}],
                "options": options,
                "source_ips": source_ips,
            },
        }
        self.pending.append((key, now, credentials, body))

    def tick(self, jobs):
        if jobs.active or jobs.pending or jobs.closing or jobs.controls.awaiting_confirmation():
            return
        while self.pending:
            key, when, credentials, body = self.pending.popleft()
            run = jobs.long_ping.run
            if (
                time.monotonic() - when <= 60
                and credentials is jobs.operations.credentials
                and run
                and run.key == key
                and run.state in {"running", "done"}
            ):
                jobs.submit("ping.trace", body)
                return

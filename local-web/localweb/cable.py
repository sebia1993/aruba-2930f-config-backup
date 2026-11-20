"""Selected-port ArubaOS-Switch TDR measurements on the existing pinned SSH connection."""

import re
from contextlib import ExitStack
from functools import partial

from aruba2930f_backup.models import CollectionFailure, ErrorCode
from aruba2930f_backup.port_control import validate_interface
from aruba2930f_backup.ssh import (
    NetmikoSSHSession,
    _clean_channel_text,
    _ends_with_prompt,
    _enforce_output_bounds,
    _has_command_echo,
    _mapped_failure,
    _strip_command_and_prompt,
)
from aruba2930f_backup.validation import validate_cli_response

_CONTINUE = re.compile(r"\bContinue\s*(?:\(y/n\)|\[y/n\])\s*\?\s*$", re.I)
_LINK_WARNING = re.compile(
    r"loss of link|link (?:loss|down|interruption)|interruption in connectivity", re.I
)
_TEST_ERROR = re.compile(
    r"^\s*(?:%\s*)?(?:error\b|invalid (?:port|interface)|(?:test|cable diagnostics?)"
    r".{0,30}(?:failed|aborted|rejected|cancelled)|unable to|cannot |could not )",
    re.I | re.M,
)
_UNSUPPORTED = re.compile(r"does not support cable diagnostics|not supported|unsupported", re.I)
_PENDING = re.compile(
    r"\bincomplete\b|\bpending\b|in progress|not (?:(?:yet|been) )*complete(?:d)?|still (?:running|being tested)",
    re.I,
)
_NO_RESULT = re.compile(
    r"\bno\b.{0,50}\b(?:results?|tests?|data)\b|"
    r"(?:never|not) (?:yet )?(?:tested|run|performed)|not been (?:run|tested|performed)",
    re.I,
)
_COMPLETE = re.compile(
    r"^\s*(?:cable\s+)?(?:diagnostics?(?:\s+test)?|test)\s+(?:(?:has|is)\s+)?"
    r"complet(?:e|ed)(?:\s+successfully)?[.!]?\s*$",
    re.I | re.M,
)
_CLEAR_ACK = re.compile(
    r"(?:(?:cable\s+)?diagnostics?(?:\s+(?:results?|buffer))?|results? buffer)\s+"
    r"(?:(?:has|have) been |was |were |is |are )?(?:successfully )?cleared(?: successfully)?[.!]?",
    re.I,
)
_PAIR = re.compile(
    r"^\s*(?:(?P<port>[\w/]+)\s+)?(?P<pair>1-2|3-6|4-5|7-8)\s+"
    r"(?P<status>[a-zA-Z][a-zA-Z /-]{0,24}?)\s+"
    r"(?P<distance>(?:[<>~]\s*)?\d+(?:\.\d+)?\s*(?:m|ft)|n/a|--?)(?=\s|$)",
    re.I,
)


_PAIR_IDS = ("1-2", "3-6", "4-5", "7-8")
_ROW = re.compile(r"^\s*(?:(?P<port>[\w/]+)\s+)?(?P<pair>[1-8]-[1-8])(?:\s|$)")
_PORT_ID = r"[a-z]?\d+(?:/[a-z]?\d+)*"
_PORT_LINE = re.compile(rf"^\s*(?:(?:port|interface)\s+)?({_PORT_ID})(?:[\s:]+|$)", re.I)
_MENTIONED_PORT = re.compile(rf"\b(?:port|interface)\s+({_PORT_ID})(?![\w/])", re.I)
_WARNING = re.compile(r"^\s*(?:%\s*)?warning\b", re.I | re.M)
_UNAVAILABLE_PAIR_STATUS = {
    "n/a",
    "not available",
    "not measured",
    "test failed",
    "unavailable",
    "unknown",
    "untested",
}
_PRIVATE_LINE = re.compile(
    r"[#>]\s*$|\b(?:password|passphrase|credentials?|secret|token|community|private key|"
    r"username|user name|authorization|api[_ -]?key)\b|^\s*(?:show |configure |ssh |ssh@)",
    re.I,
)
_ADDRESS = re.compile(
    r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])|\b(?:[\da-f]{2}[:-]){5}[\da-f]{2}\b", re.I
)


def _reading(status, message, pairs=()):
    return {"status": status, "message": message, "pairs": list(pairs)}


def parse_result(text, interface):
    reading = _parse_response(text, interface)
    if reading["status"] == "unrecognized":
        reading["status"] = "failed"
    return reading


def _parse_response(text, interface):
    """Rows without a port belong to the preceding explicit port, including on VSF."""
    current = None
    pairs = {}
    seen = set()
    uncertain = set()
    waiting_pair = False
    unavailable_pair = False
    notices = []
    excerpt = []
    for line in _clean_channel_text(text).splitlines():
        row = _ROW.match(line)
        port_line = _PORT_LINE.match(line)
        mention = _MENTIONED_PORT.search(line)
        if row and row["port"]:
            current = row["port"]
        elif port_line:
            # A vendor-specific first row still starts a different port's block.
            current = port_line[1]
        line_port = mention[1] if mention else current
        if not mention and _WARNING.match(line):
            # A CLI warning without a named port applies to the whole response.
            line_port = None
        if line_port is not None and line_port.casefold() != interface.casefold():
            continue
        if not row:
            # Device diagnostics may qualify the notice with "Port <id>:".
            notices.append(line[port_line.end() :] if port_line else line)
        if line.strip() and len(excerpt) < 12 and not _PRIVATE_LINE.search(line):
            safe_line = _ADDRESS.sub("[주소]", line)
            excerpt.append(
                "".join(c for c in safe_line[:180] if c.isprintable() or c == "\t").rstrip()
            )
        if (
            not row
            or not current
            or current.casefold() != interface.casefold()
            or row["pair"] not in _PAIR_IDS
        ):
            continue
        pair = row["pair"]
        match = _PAIR.match(line)
        if match:
            status = " ".join(match["status"].lower().split())
            if _PENDING.search(status):
                waiting_pair = True
                match = None
            elif _NO_RESULT.search(status) or status in _UNAVAILABLE_PAIR_STATUS:
                unavailable_pair = True
                match = None
        if pair in seen or not match:
            uncertain.add(pair)
            pairs.pop(pair, None)
        else:
            pairs[pair] = {"pair": pair, "status": match["status"], "distance": match["distance"]}
        seen.add(pair)

    notice = "\n".join(notices)
    pending = waiting_pair or _PENDING.search(notice)
    if _UNSUPPORTED.search(notice):
        return _reading("unsupported", "스위치가 이 포트의 케이블 진단을 지원하지 않습니다.")
    if _TEST_ERROR.search(notice):
        return _reading("failed", "스위치가 이 포트의 케이블 진단에 실패했습니다.")
    if _NO_RESULT.search(notice):
        return {**_reading("empty", "이번 검사 결과가 아직 없습니다."), "invalidatesPairs": True}
    if pending and (not seen or len(pairs) == len(_PAIR_IDS)):
        # A full table explicitly marked unfinished cannot establish a completed readback.
        return _reading("pending", "스위치에서 케이블 테스트를 진행 중입니다.")
    if not pending and _WARNING.search(notice):
        return _reading("failed", "스위치 경고로 이번 측정값을 확인할 수 없습니다.")
    if len(pairs) == len(_PAIR_IDS):
        return _reading("complete", "네 페어의 측정값을 확인했습니다.", pairs.values())
    if pairs or (uncertain and (pending or unavailable_pair)):
        # Unusable rows can withdraw a pair reported by an earlier poll of this test.
        result = _reading("partial", "일부 페어의 측정값을 확인하지 못했습니다.", pairs.values())
    elif excerpt:
        result = _reading(
            "unrecognized", "결과를 수신했지만 이 포트의 출력 형식을 해석하지 못했습니다."
        )
    else:
        result = _reading("unrecognized", "결과에서 이 포트에 해당하는 출력 행을 찾지 못했습니다.")
    if pending:
        result["stillRunning"] = True
    if uncertain:
        result["unresolved"] = sorted(uncertain)
    if excerpt:
        result["responseExcerpt"] = "\n".join(excerpt)
    return result


def _check_cancel(cancel):
    if cancel.is_set():
        raise CollectionFailure(ErrorCode.CANCELLED, "작업을 취소했습니다.")


class UncertainTest(Exception):
    """The test may already have affected a link. Never replay the transaction."""


class UnconfirmedCableRequest(UncertainTest):
    """The selected test was confirmed; only its readback may be retried on a new channel."""


def _confirmation_reply(clean, prompt):
    lines = clean.strip().splitlines()
    prompts = {prompt, prompt * 2}
    if lines:
        first = lines[0].strip()
        # A single-key console may print its prompt directly against the echoed Y.
        if first[:1].lower() == "y" and (not first[1:].strip() or first[1:].strip() in prompts):
            lines[0] = first[1:].strip()
    # The trailing Enter can redisplay the same EXEC prompt.
    synchronized = bool(lines and lines[-1].strip() in prompts)
    while lines and (not lines[-1].strip() or lines[-1].strip() in prompts):
        lines.pop()
    return "\n".join(lines).strip(), synchronized


def _missing_confirmation(command, raw, prompt):
    output, _ = _confirmation_reply(_clean_channel_text(raw), prompt)
    try:
        validate_cli_response(command, output)
    except CollectionFailure as exc:
        raise CollectionFailure(
            ErrorCode.PROMPT_PARSE_FAILED,
            "스위치가 테스트를 거부했고 SSH 프롬프트가 복구되지 않았습니다.",
        ) from exc
    if _UNSUPPORTED.search(output) or _TEST_ERROR.search(output):
        raise CollectionFailure(
            ErrorCode.PROMPT_PARSE_FAILED,
            "스위치가 테스트 실패 또는 미지원을 알렸고 SSH 프롬프트가 복구되지 않았습니다.",
        )
    if output:
        interface = command.rsplit(" ", 1)[-1]
        if (
            any(port != interface for port in _MENTIONED_PORT.findall(output))
            or any(line.rstrip().endswith(("?", "#", ">", ":")) for line in output.splitlines())
            or not (
                _COMPLETE.fullmatch(output)
                or (
                    _PENDING.search(output)
                    and re.fullmatch(r"(?:test|cable diagnostics?)\b[^\n]{0,180}", output, re.I)
                )
                or parse_result(output, interface)["pairs"]
            )
        ):
            raise UncertainTest
    raise UnconfirmedCableRequest


class CableSession(NetmikoSSHSession):
    def _command(self, command, prompt, cancel, *, next_prompt=None):
        connection = self._require_connection()
        _check_cancel(cancel)
        connection.clear_buffer()
        connection.write_channel(connection.normalize_cmd(command))
        end_prompt = next_prompt or prompt
        raw = self._read_until_prompt(
            end_prompt,
            cancel_event=cancel,
            command=None if next_prompt else command,
            check_rejection_for=command,
        )
        # On mode changes the command is echoed at the old prompt and finishes at the new one.
        if not _has_command_echo(raw, command, prompt):
            raise CollectionFailure(
                ErrorCode.PROMPT_PARSE_FAILED, "명령 실행 응답을 확인할 수 없습니다."
            )
        raw = raw.rstrip().removesuffix(end_prompt)
        output = _strip_command_and_prompt(raw, command, prompt)
        validate_cli_response(command, output)
        return output

    def clear_cable(self, cancel):
        prompt = self._require_cached_prompt()
        if not prompt.endswith("#"):
            raise CollectionFailure(ErrorCode.ENABLE_FAILED, "관리자 EXEC 권한이 필요합니다.")
        config_prompt = prompt[:-1] + "(config)#"
        self._command("configure terminal", prompt, cancel, next_prompt=config_prompt)
        output = self._command("clear cable-diagnostics", config_prompt, cancel).strip()
        if output and not _CLEAR_ACK.fullmatch(output):
            raise CollectionFailure(
                ErrorCode.COMMAND_REJECTED,
                "스위치의 케이블 진단 결과 초기화를 확인하지 못했습니다.",
            )
        self._command("end", config_prompt, cancel, next_prompt=prompt)

    def read_cable(self, interface, cancel):
        from netmiko.exceptions import ReadException, WriteException
        from paramiko.ssh_exception import SSHException

        command = f"show cable-diagnostics {validate_interface(interface)}"
        try:
            output = self._command(command, self._require_cached_prompt(), cancel)
        except (OSError, EOFError, ReadException, WriteException, SSHException) as exc:
            raise _mapped_failure(exc, during="command") from exc
        if not output.strip():
            return _reading("empty", "이번 검사 결과가 아직 없습니다.")
        return parse_result(output, interface)

    def test_cable(self, interface, cancel):
        command = f"test cable-diagnostics {validate_interface(interface)}"
        connection = self._require_connection()
        prompt = self._require_cached_prompt()
        if not prompt.endswith("#"):
            raise CollectionFailure(ErrorCode.ENABLE_FAILED, "관리자 EXEC 권한이 필요합니다.")
        _check_cancel(cancel)
        connection.clear_buffer()
        connection.write_channel(connection.normalize_cmd(command))
        warning = self._response(command, prompt, cancel, confirm=False)
        if not _CONTINUE.search(warning):
            return warning
        _check_cancel(cancel)
        try:
            connection.write_channel(connection.normalize_cmd("y"))
        except Exception as exc:
            raise UncertainTest from exc
        result = self._response(command, prompt, cancel, confirm=True)
        validate_cli_response(command, result)
        return result

    def _response(self, command, prompt, cancel, *, confirm):
        from netmiko.exceptions import ReadException
        from paramiko.ssh_exception import SSHException

        connection = self._require_connection()
        raw = ""
        deadline = self._clock() + self._options.command_timeout_seconds
        while self._clock() < deadline:
            _check_cancel(cancel)
            try:
                chunk = connection.read_channel()
            except OSError, EOFError, ReadException, SSHException:
                if confirm:
                    _missing_confirmation(command, raw, prompt)
                raise
            if chunk:
                raw += chunk
                _enforce_output_bounds(
                    raw,
                    max_bytes=self._options.max_output_bytes,
                    max_lines=self._options.max_output_lines,
                )
            elif raw:
                clean = _clean_channel_text(raw)
                if confirm:
                    output, synchronized = _confirmation_reply(clean, prompt)
                    if synchronized:
                        return output
                elif _has_command_echo(clean, command, prompt):
                    output = _strip_command_and_prompt(clean, command, prompt)
                    if _ends_with_prompt(clean, prompt):
                        validate_cli_response(command, output)
                        interface = command.rsplit(" ", 1)[-1]
                        inline = _parse_response(output, interface)
                        if (
                            inline["status"] in {"unsupported", "pending", "failed"}
                            or inline.get("stillRunning")
                            or inline["pairs"]
                            or _COMPLETE.search(output)
                        ):
                            return output
                        raise CollectionFailure(
                            ErrorCode.COMMAND_REJECTED,
                            "테스트 실행 확인 프롬프트를 받지 못했습니다.",
                        )
                    if _CONTINUE.search(clean) and _LINK_WARNING.search(" ".join(output.split())):
                        validate_cli_response(command, output)
                        return output
            self._sleep(0.05)
        if confirm:
            _missing_confirmation(command, raw, prompt)
        raise CollectionFailure(
            ErrorCode.COMMAND_TIMEOUT, "스위치에서 테스트 응답을 받지 못했습니다."
        )


def _update_row(row, reading):
    # Never carry an excerpt from a previous stage into a completed result.
    row.pop("responseExcerpt", None)
    row.update(reading)


def _start_test(session, row, cancel, report):
    interface = row["interface"]
    _check_cancel(cancel)
    row.update(stage="test", status="pending", message="테스트 요청 후 응답 대기")
    report(cable_row=row.copy())
    output = session.test_cable(interface, cancel)
    inline = _parse_response(output, interface)
    if inline["status"] in {"unsupported", "failed"}:
        return inline
    if (
        output.strip()
        and inline["status"] not in {"pending", "partial", "complete"}
        and not _COMPLETE.search(output)
    ):
        if inline.get("responseExcerpt"):
            row["responseExcerpt"] = inline["responseExcerpt"]
        raise UncertainTest("스위치가 보낸 테스트 응답 형식을 확인하지 못했습니다.")
    return None


def _read_measurement(connection, row, cancel, report, *, can_reconnect):
    interface = row["interface"]
    row.update(stage="show", status="pending", message="이번 검사 결과를 조회하고 있습니다.")
    report(cable_row=row.copy())
    # Only the readback after this switch's clear and this port's test contributes pairs.
    pairs = {}
    still_running = False
    for attempt in range(16):
        _check_cancel(cancel)
        try:
            result = connection.session.read_cable(interface, cancel)
        except CollectionFailure as exc:
            if not can_reconnect or exc.code not in {
                ErrorCode.COMMAND_TIMEOUT,
                ErrorCode.SSH_NEGOTIATION_FAILED,
            }:
                raise
            connection.reconnect_for_readback(row, cancel, report)
            can_reconnect = False
            _check_cancel(cancel)
            result = connection.session.read_cable(interface, cancel)
        _check_cancel(cancel)
        # A blank poll supplies no new execution state.
        if result["status"] != "empty" or result.get("invalidatesPairs"):
            still_running = result["status"] == "pending" or bool(result.get("stillRunning"))
        if result.get("invalidatesPairs") and (pairs or row["status"] == "partial"):
            pairs.clear()
            _update_row(
                row, _reading("pending", "장비가 이번 검사 결과가 없다고 응답해 다시 조회합니다.")
            )
            report(cable_row=row.copy())
        if result["status"] == "complete":
            return _reading("measured", "이번 검사에서 측정한 결과입니다.", result["pairs"])
        for pair in result.get("unresolved", ()):
            pairs.pop(pair, None)
        if result["status"] == "partial":
            for item in result["pairs"]:
                pairs[item["pair"]] = item
            confirmed = [pairs[pair] for pair in _PAIR_IDS if pair in pairs]
            message = "일부 페어의 측정값을 확인하지 못했습니다."
            if len(confirmed) == len(_PAIR_IDS):
                if not result.get("stillRunning"):
                    return _reading("measured", "이번 검사에서 측정한 결과입니다.", confirmed)
                message = "네 페어를 수신했으며 장비의 테스트 완료 확인을 기다리고 있습니다."
            partial = _reading("partial", message, confirmed)
            if result.get("responseExcerpt"):
                partial["responseExcerpt"] = result["responseExcerpt"]
            if any(row.get(k) != partial.get(k) for k in (*partial, "responseExcerpt")):
                _update_row(row, partial)
                report(cable_row=row.copy())
        elif result["status"] not in {"empty", "pending"}:
            failed = _reading(
                result["status"], result["message"], [pairs[k] for k in _PAIR_IDS if k in pairs]
            )
            if result.get("responseExcerpt"):
                failed["responseExcerpt"] = result["responseExcerpt"]
            return failed
        if attempt < 15 and cancel.wait(1):
            _check_cancel(cancel)
    if still_running:
        result = _reading(
            "partial" if pairs else "uncertain",
            "조회 대기 시간 동안 스위치가 테스트 진행 중이라고 응답해 완료 확인을 마치지 못했습니다.",
            [pairs[k] for k in _PAIR_IDS if k in pairs],
        )
        if row.get("responseExcerpt"):
            result["responseExcerpt"] = row["responseExcerpt"]
        result["stillRunning"] = True
        return result
    if row["status"] == "partial":
        return {
            key: row[key] for key in ("status", "message", "pairs", "responseExcerpt") if key in row
        }
    raise UncertainTest


class _SwitchConnection:
    """Keep subsequent selected ports on the currently verified SSH connection."""

    def __init__(self, sessions, connect):
        self._sessions = sessions
        self._connect = connect
        self.session = sessions.enter_context(connect())

    def reconnect_for_readback(self, row, cancel, report):
        _check_cancel(cancel)
        row.update(stage="show", message="스위치 응답이 끊겨 해당 포트의 결과만 다시 조회합니다.")
        report(cable_row=row.copy())
        self._sessions.close()
        _check_cancel(cancel)
        try:
            self.session = self._sessions.enter_context(self._connect())
        except Exception as exc:
            detail = (
                exc.safe_message
                if isinstance(exc, CollectionFailure)
                else "스위치와 통신할 수 없습니다."
            )
            raise UncertainTest("결과 조회용 SSH 재연결 실패 · " + detail) from exc


def diagnose_ports(groups, open_session, cancel, progress):
    rows = []
    for group in groups:
        target = group.target
        device_rows = [
            dict(
                ip=target.ip,
                ssh_port=target.port,
                interface=port,
                stage="clear",
                **_reading("skipped", "앞선 포트의 통신이 종료되어 실행을 건너뛰었습니다."),
            )
            for port in group.interfaces
        ]
        completed = 0
        try:
            _check_cancel(cancel)
            with ExitStack() as sessions:
                connection = _SwitchConnection(sessions, partial(open_session, target))
                progress(f"{target.endpoint} · 진단 결과 초기화 중")
                connection.session.clear_cable(cancel)
                for row in device_rows:
                    row["stage"] = "test"
                for row in device_rows:
                    _check_cancel(cancel)
                    message = f"{target.endpoint} · 포트 {row['interface']} 케이블 진단"
                    report = partial(progress, message)
                    try:
                        can_reconnect = True
                        try:
                            result = _start_test(connection.session, row, cancel, report)
                        except UnconfirmedCableRequest:
                            connection.reconnect_for_readback(row, cancel, report)
                            can_reconnect = False
                            result = None
                        if result is None:
                            result = _read_measurement(
                                connection, row, cancel, report, can_reconnect=can_reconnect
                            )
                        still_running = result.pop("stillRunning", False)
                        _update_row(row, result)
                        if still_running:
                            for untested in device_rows[completed + 1 :]:
                                untested["message"] = (
                                    "앞선 포트의 테스트가 진행 중이어서 실행을 건너뛰었습니다."
                                )
                            break
                    except UncertainTest as exc:
                        row["status"] = "cancelled" if cancel.is_set() else "uncertain"
                        if cancel.is_set():
                            row["message"] = "작업을 취소했습니다."
                        elif str(exc):
                            row["message"] = str(exc)
                        elif row["stage"] == "test":
                            row["message"] = "테스트 요청 후 스위치 응답을 확인하지 못했습니다."
                        else:
                            row["message"] = "조회 대기 시간 안에 이번 검사 결과를 받지 못했습니다."
                        break
                    except CollectionFailure as exc:
                        if exc.code is not ErrorCode.COMMAND_REJECTED:
                            raise
                        row.update(status="failed", message=exc.safe_message)
                    report(finished=True, cable_row=row.copy())
                    completed += 1
        except Exception as exc:
            status = "cancelled" if cancel.is_set() else "failed"
            detail = (
                exc.safe_message
                if isinstance(exc, CollectionFailure)
                else "스위치와 통신할 수 없습니다."
            )
            for row in device_rows[completed:]:
                if row["stage"] == "clear":
                    row.update(status=status, message="초기화 실패 · " + detail)
                elif row is device_rows[completed] or cancel.is_set():
                    row.update(status=status, message=detail)
        if cancel.is_set():
            for row in device_rows[completed:]:
                row.update(status="cancelled", message="작업을 취소했습니다.")
        for row in device_rows[completed:]:
            progress(
                f"{target.endpoint} 케이블 진단 처리 완료", finished=True, cable_row=row.copy()
            )
        rows.extend(device_rows)
    return {"rows": rows}

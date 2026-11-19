"""Selected-port ArubaOS-Switch TDR measurements on the existing pinned SSH connection."""

import re

from aruba2930f_backup.models import CollectionFailure, ErrorCode
from aruba2930f_backup.port_control import validate_interface
from aruba2930f_backup.ssh import (
    NetmikoSSHSession,
    _clean_channel_text,
    _ends_with_prompt,
    _enforce_output_bounds,
    _has_command_echo,
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
    r"^\s*(?:cable\s+)?(?:diagnostic(?:s| test)?|test)\s+(?:(?:has|is)\s+)?"
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


def _reading(status, message, pairs=()):
    return {"status": status, "message": message, "pairs": list(pairs)}


def parse_result(text, interface):
    """Rows without a port belong to the preceding explicit port, including on VSF."""
    if _UNSUPPORTED.search(text):
        return _reading("unsupported", "스위치가 이 포트의 케이블 진단을 지원하지 않습니다.")
    if _TEST_ERROR.search(text):
        return _reading("failed", "스위치가 이 포트의 케이블 진단에 실패했습니다.")
    if _PENDING.search(text):
        return _reading("pending", "스위치에서 케이블 테스트를 진행 중입니다.")
    if _NO_RESULT.search(text):
        return _reading("empty", "이번 검사 결과가 아직 없습니다.")
    if re.search(r"^\s*(?:%\s*)?warning\b", text, re.I | re.M):
        return _reading("failed", "스위치 경고로 이번 측정값을 확인할 수 없습니다.")
    current = None
    pairs = {}
    ambiguous = False
    for line in text.splitlines():
        # Record a port change even if its first pair uses an unknown output format.
        first = re.match(r"^\s*([\w/]+)\s+(?=[1-8]-[1-8]\s)", line)
        if first:
            current = first[1]
        match = _PAIR.match(line)
        if not match or current != interface:
            continue
        pair = match["pair"]
        if pair in pairs:
            ambiguous = True
        pairs[pair] = {"pair": pair, "status": match["status"], "distance": match["distance"]}
    if len(pairs) == 4 and not ambiguous:
        return _reading("complete", "네 페어의 측정값을 확인했습니다.", pairs.values())
    if pairs:
        return _reading("partial", "네 페어의 측정값을 모두 확인하지 못했습니다.")
    return _reading("failed", "스위치 응답에서 이 포트의 케이블 진단 결과를 확인할 수 없습니다.")


def _check_cancel(cancel):
    if cancel.is_set():
        raise CollectionFailure(ErrorCode.CANCELLED, "작업을 취소했습니다.")


class UncertainTest(Exception):
    """The test may already have affected a link. Never replay the transaction."""


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
        command = f"show cable-diagnostics {validate_interface(interface)}"
        output = self._command(command, self._require_cached_prompt(), cancel)
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
            result = self._response("y", prompt, cancel, confirm=True)
        except Exception as exc:
            raise UncertainTest from exc
        validate_cli_response(command, result)
        return result

    def _response(self, command, prompt, cancel, *, confirm):
        connection = self._require_connection()
        raw = ""
        deadline = self._clock() + self._options.command_timeout_seconds
        while self._clock() < deadline:
            _check_cancel(cancel)
            chunk = connection.read_channel()
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
                    # Single-key firmware consumes y immediately; the following Enter
                    # can redisplay EXEC on the same line. Line-mode firmware needs it.
                    head, sep, tail = clean.rstrip().rpartition("\n")
                    if tail.strip() == prompt * 2:
                        clean = head + sep + prompt
                    if _ends_with_prompt(clean, prompt):
                        return _strip_command_and_prompt(clean, command, prompt)
                elif _has_command_echo(clean, command, prompt):
                    output = _strip_command_and_prompt(clean, command, prompt)
                    if _ends_with_prompt(clean, prompt):
                        validate_cli_response(command, output)
                        if _UNSUPPORTED.search(output) or _TEST_ERROR.search(output):
                            return output
                        raise CollectionFailure(
                            ErrorCode.COMMAND_REJECTED,
                            "테스트 실행 확인 프롬프트를 받지 못했습니다.",
                        )
                    if _CONTINUE.search(clean) and _LINK_WARNING.search(" ".join(output.split())):
                        validate_cli_response(command, output)
                        return output
            self._sleep(0.05)
        raise CollectionFailure(
            ErrorCode.COMMAND_TIMEOUT, "스위치에서 테스트 응답을 받지 못했습니다."
        )


def _measure(session, interface, cancel):
    _check_cancel(cancel)
    output = session.test_cable(interface, cancel)
    inline = parse_result(output, interface)
    if inline["status"] == "unsupported" or _TEST_ERROR.search(output):
        return inline
    if (
        output.strip()
        and inline["status"] not in {"pending", "partial", "complete"}
        and not _COMPLETE.search(output)
    ):
        raise UncertainTest
    # Results in the test response are not the readback. Only show may be repeated.
    for attempt in range(16):
        _check_cancel(cancel)
        result = session.read_cable(interface, cancel)
        _check_cancel(cancel)
        if result["status"] == "complete":
            return _reading("measured", "이번 검사에서 측정한 결과입니다.", result["pairs"])
        if result["status"] not in {"empty", "pending", "partial"}:
            return result
        if attempt < 15 and cancel.wait(1):
            _check_cancel(cancel)
    raise UncertainTest


def diagnose_ports(groups, open_session, cancel, progress):
    rows = []
    for group in groups:
        target = group.target
        device_rows = [
            dict(
                ip=target.ip,
                ssh_port=target.port,
                interface=port,
                **_reading("skipped", "앞선 포트의 통신이 종료되어 실행을 건너뛰었습니다."),
            )
            for port in group.interfaces
        ]
        completed = 0
        try:
            _check_cancel(cancel)
            with open_session(target) as session:
                progress(f"{target.endpoint} · 진단 결과 초기화 중")
                session.clear_cable(cancel)
                for row in device_rows:
                    _check_cancel(cancel)
                    interface = row["interface"]
                    message = f"{target.endpoint} · 포트 {interface} 케이블 진단"
                    progress(message)
                    try:
                        row.update(_measure(session, interface, cancel))
                    except UncertainTest:
                        row.update(
                            _reading("cancelled", "작업을 취소했습니다.")
                            if cancel.is_set()
                            else _reading(
                                "uncertain", "이번 테스트의 완료 결과를 확인하지 못했습니다."
                            )
                        )
                        break
                    except CollectionFailure as exc:
                        if exc.code is not ErrorCode.COMMAND_REJECTED:
                            raise
                        row.update(_reading("failed", exc.safe_message))
                    progress(message, finished=True)
                    completed += 1
        except Exception as exc:
            status = "cancelled" if cancel.is_set() else "failed"
            detail = (
                exc.safe_message
                if isinstance(exc, CollectionFailure)
                else "스위치와 통신할 수 없습니다."
            )
            for row in device_rows:
                if row["status"] == "skipped":
                    row.update(_reading(status, detail))
        if cancel.is_set():
            for row in device_rows:
                if row["status"] == "skipped":
                    row.update(_reading("cancelled", "작업을 취소했습니다."))
        for _ in device_rows[completed:]:
            progress(f"{target.endpoint} 케이블 진단 처리 완료", finished=True)
        rows.extend(device_rows)
    return {"rows": rows}

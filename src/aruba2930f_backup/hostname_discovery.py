"""Safe shared AOS-Switch Hostname lookup for PoE discovery and verification.

All commands are read-only and allowlisted in the actual SSH session. This
module never stores raw running-config content or authorizes a write.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .models import CollectionFailure, ErrorCode
from .validation import (
    hostname_from_prompt,
    hostname_from_running_config,
    hostname_from_system_information,
    validate_output_limits,
)

_MAX_FALLBACK_CONFIG_BYTES = 1024 * 1024
_MAX_FALLBACK_CONFIG_LINES = 10_000


class HostnameSession(Protocol):
    def send_show(self, command: str) -> str: ...

    def get_prompt(self) -> str: ...


@dataclass(frozen=True, slots=True)
class HostnameResolution:
    hostname: str | None
    source: str


def resolve_switch_hostname(
    session: HostnameSession, *, before_read: Callable[[], None] | None = None
) -> HostnameResolution:
    """Prefer official System Name; use prompt/config only if name is absent.

    An explicit `COMMAND_REJECTED` from optional reads is recoverable.
    Transport, cancellation, host-key and other failures are never hidden.
    """
    if before_read is not None:
        before_read()
    try:
        response = session.send_show("show system information")
    except CollectionFailure as exc:
        if exc.code is not ErrorCode.COMMAND_REJECTED:
            raise
    else:
        hostname = hostname_from_system_information(response)
        if hostname is not None:
            return HostnameResolution(hostname, "system-information")

    if before_read is not None:
        before_read()
    prompt_name = hostname_from_prompt(session.get_prompt())
    if prompt_name is not None:
        return HostnameResolution(prompt_name, "prompt")

    if before_read is not None:
        before_read()
    try:
        configuration = session.send_show("show running-config")
    except CollectionFailure as exc:
        if exc.code is not ErrorCode.COMMAND_REJECTED:
            raise
    else:
        try:
            validate_output_limits(
                configuration,
                max_bytes=_MAX_FALLBACK_CONFIG_BYTES,
                max_lines=_MAX_FALLBACK_CONFIG_LINES,
            )
        except CollectionFailure as exc:
            if exc.code is not ErrorCode.OUTPUT_LIMIT_EXCEEDED:
                raise
        else:
            hostname = hostname_from_running_config(configuration)
            if hostname is not None:
                return HostnameResolution(hostname, "running-config")

    return HostnameResolution(None, "unavailable")

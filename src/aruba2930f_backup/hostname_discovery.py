"""Read-only ArubaOS-Switch hostname discovery shared by query and port control.

The configured System Name is authoritative. Cached prompt text is a fallback
only; full running-config is read strictly when the other sources yield no
usable name. Never log or store the configuration output in this module.
"""

from __future__ import annotations

from collections.abc import Callable

from .models import CollectionFailure, ErrorCode
from .validation import (
    hostname_from_prompt,
    hostname_from_running_config,
    hostname_from_system_information,
)


def discover_hostname(
    send_show: Callable[[str], str],
    get_prompt: Callable[[], str],
) -> str | None:
    """Read at most one system output and, if necessary, one config output."""
    try:
        system = send_show("show system information")
    except CollectionFailure as exc:
        if exc.code is not ErrorCode.COMMAND_REJECTED:
            raise
    else:
        name = hostname_from_system_information(system)
        if name:
            return name

    prompt_name = hostname_from_prompt(get_prompt())
    if prompt_name:
        return prompt_name

    try:
        config = send_show("show running-config")
    except CollectionFailure as exc:
        if exc.code is not ErrorCode.COMMAND_REJECTED:
            raise
        return None
    return hostname_from_running_config(config)

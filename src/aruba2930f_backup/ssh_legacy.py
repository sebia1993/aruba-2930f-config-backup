"""Explicit and temporary per-endpoint compatibility with old SSH server keys.

There is no disk persistence, automatic downgrade or wildcard approval.
The `ssh-rsa` *server host-key signature* is allowed only for endpoints
explicitly approved by the operator during this application process.
"""

from __future__ import annotations

import threading

from .models import DeviceTarget


class SessionLegacySSHAllowlist:
    """Keep legacy RSA/SHA-1 server-key exceptions bounded to one process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._endpoints: set[str] = set()

    @staticmethod
    def _endpoint(target: DeviceTarget) -> str:
        if not isinstance(target, DeviceTarget):
            raise TypeError("A validated DeviceTarget is required for SSH exceptions.")
        return target.endpoint

    def allow(self, target: DeviceTarget) -> None:
        endpoint = self._endpoint(target)
        with self._lock:
            self._endpoints.add(endpoint)

    def allowed(self, target: DeviceTarget) -> bool:
        endpoint = self._endpoint(target)
        with self._lock:
            return endpoint in self._endpoints

    def revoke(self, target: DeviceTarget) -> None:
        endpoint = self._endpoint(target)
        with self._lock:
            self._endpoints.discard(endpoint)

    def clear(self) -> None:
        with self._lock:
            self._endpoints.clear()


session_legacy_ssh = SessionLegacySSHAllowlist()

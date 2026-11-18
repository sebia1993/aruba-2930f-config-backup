"""Session-only tracking of automatically negotiated legacy SSH server keys.

The pre-auth host-key probe marks an exact IP:SSH port only after a strict
host-key mismatch and a successful bounded legacy probe. No disk persistence,
network wildcard, or automatic PoE/configuration retry is permitted.
"""

from __future__ import annotations

import threading

from .models import DeviceTarget


class SessionLegacySSHAllowlist:
    """Track verified pre-auth compatibility by exact endpoint in memory."""

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

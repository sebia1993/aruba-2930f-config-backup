"""Session-scoped legacy SSH compatibility never bypasses pinned host keys."""

from __future__ import annotations

from pathlib import Path

import pytest

from aruba2930f_backup.hostkeys import (
    HostKeyStore,
    disabled_sha1_rsa_algorithms,
    sha256_fingerprint,
)
from aruba2930f_backup.models import (
    CollectionFailure,
    DeviceTarget,
    ErrorCode,
    HostKeyObservation,
)
from aruba2930f_backup.ssh_legacy import session_legacy_ssh


def test_wc1601_auto_detection_stays_scoped_to_exact_endpoint() -> None:
    old = DeviceTarget("192.0.2.10", 22)
    another = DeviceTarget("192.0.2.11", 22)
    alternate_port = DeviceTarget("192.0.2.10", 2222)
    session_legacy_ssh.clear()
    try:
        assert disabled_sha1_rsa_algorithms(old) == {"keys": ["ssh-rsa"], "pubkeys": ["ssh-rsa"]}
        session_legacy_ssh.allow(old)
        assert disabled_sha1_rsa_algorithms(old) == {"pubkeys": ["ssh-rsa"]}
        assert disabled_sha1_rsa_algorithms(another) == {
            "keys": ["ssh-rsa"],
            "pubkeys": ["ssh-rsa"],
        }
        assert disabled_sha1_rsa_algorithms(alternate_port) == {
            "keys": ["ssh-rsa"],
            "pubkeys": ["ssh-rsa"],
        }
        session_legacy_ssh.revoke(old)
        assert "keys" in disabled_sha1_rsa_algorithms(old)
    finally:
        session_legacy_ssh.clear()


def test_legacy_server_host_key_is_still_pinned_and_changed_keys_rejected(tmp_path: Path) -> None:
    target = DeviceTarget("192.0.2.15", 22)
    store = HostKeyStore(tmp_path / "known_hosts.json")
    first = HostKeyObservation(target, "ssh-rsa", sha256_fingerprint(b"expected-rsa-key"))
    changed = HostKeyObservation(target, "ssh-rsa", sha256_fingerprint(b"unexpected-rsa-key"))
    session_legacy_ssh.clear()
    try:
        session_legacy_ssh.allow(target)
        with pytest.raises(CollectionFailure) as unknown:
            store.require_trusted(first)
        assert unknown.value.code is ErrorCode.HOST_KEY_REJECTED
        store.approve([store.check(first)])
        store.require_trusted(first)
        with pytest.raises(CollectionFailure) as changed_key:
            store.require_trusted(changed)
        assert changed_key.value.code is ErrorCode.HOST_KEY_CHANGED
    finally:
        session_legacy_ssh.clear()


def test_detected_legacy_endpoint_does_not_persist_between_sessions() -> None:
    target = DeviceTarget("192.0.2.10", 22)
    session_legacy_ssh.clear()
    session_legacy_ssh.allow(target)
    assert session_legacy_ssh.allowed(target)
    session_legacy_ssh.clear()
    assert not session_legacy_ssh.allowed(target)


def test_both_windows_distributions_keep_the_reviewed_paramiko_pin() -> None:
    """A major SSH dependency update must be deliberately reviewed in both builds."""
    root = Path(__file__).resolve().parents[1]
    desktop = (root / "requirements-lock.txt").read_text(encoding="utf-8")
    web = (root / "local-web" / "requirements-runtime.txt").read_text(encoding="utf-8")

    def pinned(lines: str) -> list[str]:
        return [
            line.strip()
            for line in lines.splitlines()
            if line.strip().lower().startswith("paramiko") and not line.lstrip().startswith("#")
        ]

    assert pinned(desktop) == ["paramiko==4.0.0"]
    assert pinned(web) == pinned(desktop)


def test_legacy_ssh_downgrade_never_opens_sha1_key_exchange() -> None:
    """The narrow host-key fallback must not silently enable unsafe KEX."""
    target = DeviceTarget("192.0.2.50", 22)
    session_legacy_ssh.clear()
    try:
        initial = disabled_sha1_rsa_algorithms(target)
        assert initial == {"keys": ["ssh-rsa"], "pubkeys": ["ssh-rsa"]}
        session_legacy_ssh.allow(target)
        fallback = disabled_sha1_rsa_algorithms(target)
        assert fallback == {"pubkeys": ["ssh-rsa"]}
        assert "kex" not in fallback
        assert "ciphers" not in fallback
        assert "ssh-rsa" in fallback["pubkeys"]
    finally:
        session_legacy_ssh.clear()

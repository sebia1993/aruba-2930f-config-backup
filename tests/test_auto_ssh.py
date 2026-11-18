"""Automatic legacy host-key negotiation is bounded to pre-auth failures."""

from __future__ import annotations

import sys
import types

import pytest

from aruba2930f_backup.hostkeys import ParamikoHostKeyProbe
from aruba2930f_backup.models import CollectionFailure, DeviceTarget, ErrorCode
from aruba2930f_backup.ssh_legacy import session_legacy_ssh

TARGET = DeviceTarget("192.0.2.88", 22)


@pytest.fixture(autouse=True)
def isolate_legacy_detection():
    session_legacy_ssh.clear()
    yield
    session_legacy_ssh.clear()


def make_fake_transport(monkeypatch, *, fail_with: str | None = None):
    calls: list[dict[str, list[str]]] = []
    sockets: list[object] = []

    class FakeSocket:
        def settimeout(self, _timeout: float) -> None:
            pass

        def close(self) -> None:
            pass

    class SSHException(Exception):
        pass

    class FakeKey:
        def get_name(self) -> str:
            return "ssh-rsa"

        def asbytes(self) -> bytes:
            return b"public-rsa-key"

    class FakeTransport:
        def __init__(self, _sock: object, *, disabled_algorithms: dict[str, list[str]]) -> None:
            self.policy = disabled_algorithms

        def start_client(self, *, timeout: float) -> None:
            assert timeout > 0
            calls.append(self.policy)
            if fail_with is not None and "keys" in self.policy:
                raise SSHException(fail_with)

        def get_remote_server_key(self) -> FakeKey:
            return FakeKey()

        def close(self) -> None:
            pass

    fake = types.ModuleType("paramiko")
    fake.Transport = FakeTransport  # type: ignore[attr-defined]
    fake.SSHException = SSHException  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paramiko", fake)

    def connect(_address: object, *, timeout: float) -> FakeSocket:
        assert timeout > 0
        socket = FakeSocket()
        sockets.append(socket)
        return socket

    monkeypatch.setattr("aruba2930f_backup.hostkeys.socket.create_connection", connect)
    return calls, sockets


def test_only_hostkey_incompatibility_triggers_one_auto_reprobe(monkeypatch) -> None:
    calls, sockets = make_fake_transport(
        monkeypatch, fail_with="Incompatible ssh peer (no acceptable host key)"
    )
    observation = ParamikoHostKeyProbe().probe(TARGET, timeout=3.0)
    assert observation.key_type == "ssh-rsa"
    assert len(calls) == 2
    assert calls[0] == {"keys": ["ssh-rsa"], "pubkeys": ["ssh-rsa"]}
    assert calls[1] == {"pubkeys": ["ssh-rsa"]}
    assert len(sockets) == 2
    assert session_legacy_ssh.allowed(TARGET)
    assert not session_legacy_ssh.allowed(DeviceTarget("192.0.2.89", 22))
    assert not session_legacy_ssh.allowed(DeviceTarget(TARGET.ip, 2222))


def test_kex_mismatch_never_triggers_crypto_downgrade(monkeypatch) -> None:
    calls, _ = make_fake_transport(
        monkeypatch, fail_with="Incompatible ssh peer (no acceptable kex algorithm)"
    )
    with pytest.raises(CollectionFailure) as exc:
        ParamikoHostKeyProbe().probe(TARGET, timeout=3.0)
    assert exc.value.code is ErrorCode.SSH_ALGORITHM_INCOMPATIBLE
    assert len(calls) == 1
    assert not session_legacy_ssh.allowed(TARGET)


def test_successful_modern_probe_reverts_stale_legacy_marker(monkeypatch) -> None:
    calls, _ = make_fake_transport(monkeypatch)
    session_legacy_ssh.allow(TARGET)
    ParamikoHostKeyProbe().probe(TARGET, timeout=3.0)
    assert calls == [{"keys": ["ssh-rsa"], "pubkeys": ["ssh-rsa"]}]
    assert not session_legacy_ssh.allowed(TARGET)

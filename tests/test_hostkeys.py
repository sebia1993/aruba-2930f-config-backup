from __future__ import annotations

import json
import sys
import types

import pytest

from aruba2930f_backup.hostkeys import (
    HostKeyStore,
    ParamikoHostKeyProbe,
    _classify_probe_failure,
    sha256_fingerprint,
)
from aruba2930f_backup.models import (
    CollectionFailure,
    DeviceTarget,
    ErrorCode,
    HostKeyObservation,
    HostKeyTrustState,
)

TARGET = DeviceTarget("192.0.2.10", 22)


def observation(key: bytes = b"public-key-a", key_type: str = "ssh-ed25519") -> HostKeyObservation:
    return HostKeyObservation(TARGET, key_type, sha256_fingerprint(key))


def test_sha256_fingerprint_uses_openssh_format_without_padding() -> None:
    fingerprint = sha256_fingerprint(b"test-key")

    assert fingerprint.startswith("SHA256:")
    assert "=" not in fingerprint


def test_algorithm_incompatibility_is_safe_and_non_retryable() -> None:
    code, message, transient = _classify_probe_failure(
        RuntimeError("Incompatible ssh peer (no acceptable kex algorithm)")
    )

    assert code is ErrorCode.SSH_ALGORITHM_INCOMPATIBLE
    assert "compatible" in message
    assert transient is False


def test_host_key_probe_disables_sha1_rsa_before_transport_start(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeConnection:
        def settimeout(self, timeout: float) -> None:
            calls["socket_timeout"] = timeout

        def close(self) -> None:
            calls["socket_closed"] = True

    class FakeKey:
        def get_name(self) -> str:
            return "ssh-ed25519"

        def asbytes(self) -> bytes:
            return b"host-key"

    class FakeTransport:
        def __init__(self, connection: object, *, disabled_algorithms: object) -> None:
            calls["connection"] = connection
            calls["disabled_algorithms"] = disabled_algorithms

        def start_client(self, *, timeout: float) -> None:
            calls["start_timeout"] = timeout

        def get_remote_server_key(self) -> FakeKey:
            return FakeKey()

        def close(self) -> None:
            calls["transport_closed"] = True

    fake_paramiko = types.ModuleType("paramiko")
    fake_paramiko.Transport = FakeTransport  # type: ignore[attr-defined]
    fake_paramiko.SSHException = RuntimeError  # type: ignore[attr-defined]
    connection = FakeConnection()
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(
        "aruba2930f_backup.hostkeys.socket.create_connection",
        lambda address, timeout: (
            calls.update(address=address, connect_timeout=timeout) or connection
        ),
    )

    observed = ParamikoHostKeyProbe().probe(TARGET, timeout=7.5)

    assert calls["disabled_algorithms"] == {
        "keys": ["ssh-rsa"],
        "pubkeys": ["ssh-rsa"],
    }
    assert calls["address"] == (TARGET.ip, TARGET.port)
    assert calls["connect_timeout"] == 7.5
    assert calls["start_timeout"] == 7.5
    assert calls["transport_closed"] is True
    assert calls["socket_closed"] is True
    assert observed.key_type == "ssh-ed25519"


def test_connection_refused_is_reported_without_endpoint_details() -> None:
    code, message, transient = _classify_probe_failure(ConnectionRefusedError("private-host"))

    assert code is ErrorCode.SSH_NEGOTIATION_FAILED
    assert message == "The SSH endpoint refused the TCP connection."
    assert "private-host" not in message
    assert transient is True


def test_unknown_key_requires_explicit_approval_then_matches(tmp_path) -> None:
    path = tmp_path / "known_hosts.json"
    store = HostKeyStore(path)

    check = store.check(observation())
    assert check.state is HostKeyTrustState.UNKNOWN
    assert not path.exists()

    store.approve([check])

    assert store.check(observation()).state is HostKeyTrustState.TRUSTED
    approved = store.list_approved()
    assert len(approved) == 1
    assert approved[0].endpoint == TARGET.endpoint
    assert approved[0].key_type == "ssh-ed25519"
    assert approved[0].fingerprint == observation().fingerprint
    assert approved[0].approved_at


def test_changed_key_fails_closed_and_cannot_be_reapproved_until_removed(tmp_path) -> None:
    store = HostKeyStore(tmp_path / "known_hosts.json")
    first = store.check(observation(b"first"))
    store.approve([first])

    changed = store.check(observation(b"second"))
    assert changed.state is HostKeyTrustState.CHANGED
    assert changed.known_fingerprint == observation(b"first").fingerprint
    with pytest.raises(CollectionFailure) as required:
        store.require_trusted(changed.observation)
    assert required.value.code is ErrorCode.HOST_KEY_CHANGED
    with pytest.raises(CollectionFailure) as approval:
        store.approve([changed])
    assert approval.value.code is ErrorCode.HOST_KEY_CHANGED

    assert store.remove(TARGET)
    assert not store.remove(TARGET.endpoint)
    newly_unknown = store.check(observation(b"second"))
    assert newly_unknown.state is HostKeyTrustState.UNKNOWN
    store.approve([newly_unknown])
    assert store.check(observation(b"second")).state is HostKeyTrustState.TRUSTED


def test_key_type_change_is_treated_as_changed(tmp_path) -> None:
    store = HostKeyStore(tmp_path / "known_hosts.json")
    store.approve([store.check(observation())])

    changed = store.check(observation(key_type="ssh-rsa"))

    assert changed.state is HostKeyTrustState.CHANGED


def test_unknown_key_is_never_written_by_check_or_require(tmp_path) -> None:
    path = tmp_path / "known_hosts.json"
    store = HostKeyStore(path)

    with pytest.raises(CollectionFailure) as captured:
        store.require_trusted(observation())

    assert captured.value.code is ErrorCode.HOST_KEY_REJECTED
    assert not path.exists()


def test_corrupt_or_wrong_schema_store_fails_closed_without_overwrite(tmp_path) -> None:
    path = tmp_path / "known_hosts.json"
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(CollectionFailure) as corrupt:
        HostKeyStore(path).check(observation())
    assert corrupt.value.code is ErrorCode.HOST_KEY_REJECTED
    assert path.read_text(encoding="utf-8") == "not-json"

    path.write_text(json.dumps({"schema_version": 99, "endpoints": {}}), encoding="utf-8")
    with pytest.raises(CollectionFailure):
        HostKeyStore(path).list_approved()

    path.write_text(
        json.dumps({"schema_version": 1, "endpoints": {TARGET.endpoint: "not-a-record"}}),
        encoding="utf-8",
    )
    with pytest.raises(CollectionFailure) as invalid_record:
        HostKeyStore(path).list_approved()
    assert invalid_record.value.code is ErrorCode.HOST_KEY_REJECTED


def test_approval_is_atomic_and_store_contains_no_temporary_files(tmp_path) -> None:
    path = tmp_path / "nested" / "known_hosts.json"
    store = HostKeyStore(path)
    store.approve([store.check(observation())])

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert list(payload["endpoints"]) == [TARGET.endpoint]
    assert list(path.parent.glob("*.tmp")) == []


def test_approve_rejects_fabricated_non_unknown_review(tmp_path) -> None:
    store = HostKeyStore(tmp_path / "known_hosts.json")
    check = store.check(observation())
    fabricated = type(check)(check.observation, HostKeyTrustState.TRUSTED)

    with pytest.raises(CollectionFailure) as captured:
        store.approve([fabricated])

    assert captured.value.code is ErrorCode.HOST_KEY_REJECTED


def _unknown(address: str, key: bytes = b"public-key-a"):
    from aruba2930f_backup.models import HostKeyCheck

    return HostKeyCheck(
        HostKeyObservation(DeviceTarget(address, 22), "ssh-ed25519", sha256_fingerprint(key)),
        HostKeyTrustState.UNKNOWN,
    )


@pytest.mark.parametrize("operation", ["approve", "remove", "changed"])
def test_concurrent_store_transactions_preserve_updates(tmp_path, operation) -> None:
    """An instance-local lock must not allow stale read/merge/replace writes."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "known_hosts.json"
    first = HostKeyStore(path)
    (path.parent / "nested").mkdir()
    second = HostKeyStore(path.parent / "nested" / ".." / path.name)
    unrelated = _unknown("192.0.2.1")
    removed = _unknown("192.0.2.2")
    first.approve([unrelated, removed])
    write_entered = threading.Event()
    release_write = threading.Event()
    second_entered = threading.Event()
    second_finished = threading.Event()
    original_write = first._write_endpoints

    def held_write(endpoints):
        write_entered.set()
        assert release_write.wait(10), "transaction was not released"
        original_write(endpoints)

    first._write_endpoints = held_write
    added = _unknown("192.0.2.3")

    def competing_update():
        second_entered.set()
        try:
            if operation == "remove":
                return second.remove(removed.observation.target)
            check = (
                _unknown("192.0.2.3", b"changed")
                if operation == "changed"
                else _unknown("192.0.2.4")
            )
            second.approve([check])
            return None
        finally:
            second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(first.approve, [added])
        try:
            assert write_entered.wait(5)
            contender = pool.submit(competing_update)
            assert second_entered.wait(5)
            # The old implementation finishes here with a stale snapshot. A
            # correct transaction waits for the held writer; no sleep is used.
            second_finished.wait(0.5)
        finally:
            release_write.set()
        writer.result(timeout=5)
        if operation == "changed":
            with pytest.raises(CollectionFailure) as captured:
                contender.result(timeout=5)
            assert captured.value.code is ErrorCode.HOST_KEY_CHANGED
        else:
            result = contender.result(timeout=5)
            if operation == "remove":
                assert result is True

    expected = {"192.0.2.1:22", "192.0.2.3:22"}
    if operation != "remove":
        expected.add("192.0.2.2:22")
    if operation == "approve":
        expected.add("192.0.2.4:22")
    assert {key.endpoint for key in first.list_approved()} == expected
    assert first.check(added.observation).state is HostKeyTrustState.TRUSTED


def _stop_children(*children) -> None:
    for child in children:
        if child.pid is not None:
            child.join(timeout=5)
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
            assert not child.is_alive()
            child.close()


def test_spawned_process_approvals_do_not_lose_updates(tmp_path) -> None:
    import multiprocessing

    from tests.hostkey_process_worker import approve_in_child

    context = multiprocessing.get_context("spawn")
    path = tmp_path / "known_hosts.json"
    entered_a, entered_b, write_entered, release = [context.Event() for _ in range(4)]
    finished_a, finished_b = context.Event(), context.Event()
    result_a, send_a = context.Pipe(duplex=False)
    result_b, send_b = context.Pipe(duplex=False)
    first = context.Process(
        target=approve_in_child,
        args=(
            str(path),
            "192.0.2.1",
            entered_a,
            finished_a,
            send_a,
            write_entered,
            release,
        ),
    )
    second = context.Process(
        target=approve_in_child,
        args=(
            str(path),
            "192.0.2.2",
            entered_b,
            finished_b,
            send_b,
        ),
    )
    try:
        first.start()
        assert write_entered.wait(10)
        second.start()
        assert entered_b.wait(10)
        finished_b.wait(0.5)
        release.set()
        assert finished_a.wait(10)
        assert finished_b.wait(10)
        assert result_a.poll(1) and result_a.recv()[0] == "approved"
        assert result_b.poll(1) and result_b.recv()[0] == "approved"
    finally:
        release.set()
        _stop_children(first, second)
        for connection in (result_a, result_b, send_a, send_b):
            connection.close()

    assert {key.endpoint for key in HostKeyStore(path).list_approved()} == {
        "192.0.2.1:22",
        "192.0.2.2:22",
    }


@pytest.mark.parametrize("operation", ["approve", "remove"])
def test_store_busy_timeout_is_safe_and_preserves_records(tmp_path, monkeypatch, operation) -> None:
    import multiprocessing
    import time

    from tests.hostkey_process_worker import hold_lock_in_child

    path = tmp_path / "known_hosts.json"
    store = HostKeyStore(path)
    store.approve([store.check(observation())])
    before = path.read_bytes()
    context = multiprocessing.get_context("spawn")
    acquired, release = context.Event(), context.Event()
    holder = context.Process(target=hold_lock_in_child, args=(str(path), acquired, release))
    monkeypatch.setattr("aruba2930f_backup.hostkey_lock._LOCK_WAIT_SECONDS", 0.15)
    try:
        holder.start()
        assert acquired.wait(10)
        start = time.monotonic()
        with pytest.raises(CollectionFailure) as captured:
            if operation == "approve":
                store.approve([_unknown("192.0.2.2")])
            else:
                store.remove(TARGET)
        assert captured.value.code is ErrorCode.HOST_KEY_REJECTED
        assert str(path) not in str(captured.value)
        assert time.monotonic() - start < 2
        assert path.read_bytes() == before
    finally:
        release.set()
        _stop_children(holder)

    # Timeout must release the caller's descriptor and not poison future work.
    assert store.remove(TARGET)


def test_crashed_process_does_not_leave_a_stale_lock(tmp_path) -> None:
    import multiprocessing

    from tests.hostkey_process_worker import hold_lock_in_child

    path = tmp_path / "known_hosts.json"
    context = multiprocessing.get_context("spawn")
    acquired, release = context.Event(), context.Event()
    holder = context.Process(target=hold_lock_in_child, args=(str(path), acquired, release))
    try:
        holder.start()
        assert acquired.wait(10)
        holder.terminate()
        holder.join(timeout=5)
        assert not holder.is_alive()
        store = HostKeyStore(path)
        store.approve([store.check(observation())])
        assert store.check(observation()).state is HostKeyTrustState.TRUSTED
    finally:
        # A terminated process may die inside Event.wait's condition protocol;
        # never reuse that synchronization object after forced process death.
        _stop_children(holder)


@pytest.mark.parametrize("operation", ["approve", "remove"])
@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
def test_transaction_write_failure_preserves_approved_records(
    tmp_path,
    monkeypatch,
    operation,
    failure_point,
) -> None:
    from pathlib import Path

    path = tmp_path / "known_hosts.json"
    store = HostKeyStore(path)
    store.approve([store.check(observation())])
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("private filesystem details")

    if failure_point == "fsync":
        monkeypatch.setattr("aruba2930f_backup.hostkeys.os.fsync", fail)
    else:
        monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(CollectionFailure) as captured:
        if operation == "approve":
            store.approve([_unknown("192.0.2.2")])
        else:
            store.remove(TARGET)
    assert captured.value.code is ErrorCode.HOST_KEY_REJECTED
    assert "private filesystem details" not in str(captured.value)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("operation", ["approve", "remove"])
def test_corrupt_store_cannot_be_overwritten_by_transactions(tmp_path, operation) -> None:
    path = tmp_path / "known_hosts.json"
    path.write_text("damaged store", encoding="utf-8")
    store = HostKeyStore(path)
    with pytest.raises(CollectionFailure) as captured:
        if operation == "approve":
            store.approve([_unknown("192.0.2.2")])
        else:
            store.remove(TARGET)
    assert captured.value.code is ErrorCode.HOST_KEY_REJECTED
    assert path.read_text(encoding="utf-8") == "damaged store"


@pytest.mark.parametrize("operation", ["approve", "remove"])
def test_lock_io_failure_is_safe_and_preserves_store(tmp_path, monkeypatch, operation) -> None:
    path = tmp_path / "known_hosts.json"
    store = HostKeyStore(path)
    store.approve([store.check(observation())])
    before = path.read_bytes()

    def fail(handle):
        raise OSError("private lock details")

    monkeypatch.setattr("aruba2930f_backup.hostkey_lock._try_lock", fail)
    with pytest.raises(CollectionFailure) as captured:
        if operation == "approve":
            store.approve([_unknown("192.0.2.2")])
        else:
            store.remove(TARGET)
    assert captured.value.code is ErrorCode.HOST_KEY_REJECTED
    assert "private lock details" not in str(captured.value)
    assert path.read_bytes() == before


def test_file_symlink_alias_approval_and_removal_share_one_store(tmp_path) -> None:
    """Atomic replacement through an alias must update its canonical store."""
    path = tmp_path / "known_hosts.json"
    target_store = HostKeyStore(path)
    original = _unknown("192.0.2.1")
    target_store.approve([original])
    alias = tmp_path / "alias.json"
    try:
        alias.symlink_to(path.name)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable in this environment: {exc}")
    alias_store = HostKeyStore(alias)
    alias_added = _unknown("192.0.2.2")
    target_added = _unknown("192.0.2.3")

    alias_store.approve([alias_added])
    assert target_store.check(alias_added.observation).state is HostKeyTrustState.TRUSTED
    target_store.approve([target_added])
    assert alias_store.check(target_added.observation).state is HostKeyTrustState.TRUSTED
    assert alias.is_symlink()
    expected = {"192.0.2.1:22", "192.0.2.2:22", "192.0.2.3:22"}
    assert {key.endpoint for key in target_store.list_approved()} == expected
    assert alias_store.list_approved() == target_store.list_approved()

    assert alias_store.remove(original.observation.target)
    assert target_store.check(original.observation).state is HostKeyTrustState.UNKNOWN
    assert target_store.remove(alias_added.observation.target)
    assert alias_store.check(alias_added.observation).state is HostKeyTrustState.UNKNOWN
    assert alias_store.list_approved() == target_store.list_approved()
    assert {key.endpoint for key in alias_store.list_approved()} == {"192.0.2.3:22"}
    assert alias.is_symlink()


def test_store_path_resolution_failure_is_safe(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    def fail(path, *args, **kwargs):
        raise OSError("private resolution details")

    monkeypatch.setattr(Path, "resolve", fail)
    with pytest.raises(CollectionFailure) as captured:
        HostKeyStore(tmp_path / "known_hosts.json")
    assert captured.value.code is ErrorCode.HOST_KEY_REJECTED
    assert "private resolution details" not in str(captured.value)

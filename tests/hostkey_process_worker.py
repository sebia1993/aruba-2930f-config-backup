"""Spawn-safe workers for host-key transaction integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
from aruba2930f_backup.models import (
    CollectionFailure,
    DeviceTarget,
    HostKeyCheck,
    HostKeyObservation,
    HostKeyTrustState,
)


def approve_in_child(
    path: str,
    address: str,
    entered: Any,
    finished: Any,
    result: Any,
    write_entered: Any = None,
    release_write: Any = None,
) -> None:
    """Optionally hold a real transaction immediately before its atomic write."""
    store = HostKeyStore(path)
    check = HostKeyCheck(
        HostKeyObservation(DeviceTarget(address, 22), "ssh-ed25519", sha256_fingerprint(b"key")),
        HostKeyTrustState.UNKNOWN,
    )
    if write_entered is not None:
        original_write = store._write_endpoints

        def held_write(endpoints: dict[str, dict[str, Any]]) -> None:
            write_entered.set()
            if not release_write.wait(15):
                raise RuntimeError("parent did not release the transaction")
            original_write(endpoints)

        store._write_endpoints = held_write  # type: ignore[method-assign]
    entered.set()
    try:
        store.approve([check])
    except CollectionFailure as exc:
        result.send(("failure", exc.code.value))
    else:
        result.send(("approved", address))
    finally:
        finished.set()
        result.close()


def hold_lock_in_child(path: str, acquired: Any, release: Any) -> None:
    # Import here so the worker remains importable during the initial RED run.
    from aruba2930f_backup.hostkey_lock import host_key_transaction

    with host_key_transaction(Path(path)):
        acquired.set()
        if not release.wait(15):
            raise RuntimeError("parent did not release the lock")

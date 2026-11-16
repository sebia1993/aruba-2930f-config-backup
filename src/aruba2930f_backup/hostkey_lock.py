"""Bounded OS transactions for a host-key store shared by application instances."""

from __future__ import annotations

import errno
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO

from .models import CollectionFailure, ErrorCode

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_LOCK_WAIT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.05


def _try_lock(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def host_key_transaction(path: Path) -> Iterator[None]:
    """Lock a stable sibling inode across the complete read/merge/replace cycle.

    The lock file deliberately persists: deleting it could allow another process
    to lock a different inode while a previous holder still owns the old one.
    Ownership belongs to the OS descriptor and is released even on process death.
    """
    handle: BinaryIO | None = None
    acquired = False
    try:
        canonical = path.resolve()
        canonical.parent.mkdir(parents=True, exist_ok=True)
        handle = (canonical.parent / f".{canonical.name}.lock").open("a+b", buffering=0)
        # Windows byte-range locks require a byte; concurrent initialization
        # writes the same harmless value before either process acquires the lock.
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                _try_lock(handle)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CollectionFailure(
                        ErrorCode.HOST_KEY_REJECTED,
                        "The approved host-key store is busy; try again after the other operation finishes.",
                    ) from exc
                time.sleep(min(_LOCK_POLL_SECONDS, remaining))
            else:
                acquired = True
                break
    except OSError as exc:
        if handle is not None:
            handle.close()
        raise CollectionFailure(
            ErrorCode.HOST_KEY_REJECTED,
            "The approved host-key store could not be locked safely.",
        ) from exc
    except BaseException:
        if handle is not None:
            handle.close()
        raise

    try:
        yield
    finally:
        if acquired:
            with suppress(OSError):
                _unlock(handle)
        handle.close()

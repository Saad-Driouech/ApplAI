"""Exclusive per-phase locks, shared by API worker threads and CLI runs."""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class PhaseBusy(RuntimeError):
    """Another run of this pipeline phase is in progress."""


@contextmanager
def phase_lock(lock_dir: Path, phase: str) -> Iterator[None]:
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = open(lock_dir / f"{phase}.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise PhaseBusy(f"Pipeline phase '{phase}' is already running") from None
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()

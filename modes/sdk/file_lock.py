from __future__ import annotations

import os
from typing import IO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def lock_file(fh: IO[str], *, shared: bool) -> None:
    if os.name == "nt":
        # msvcrt does not provide shared locks; use an exclusive lock for both modes.
        fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        return
    lock_type = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    fcntl.flock(fh.fileno(), lock_type)


def unlock_file(fh: IO[str]) -> None:
    if os.name == "nt":
        fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

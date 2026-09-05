"""OS-held ownership, released automatically on process exit; no stale PID guessing."""
from contextlib import contextmanager
import os

from .errors import AppError


@contextmanager
def exclusive_directory(root):
    with (root / ".meow.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"\0")
            handle.flush()
            handle.seek(0)
            lock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(handle, fcntl.LOCK_UN)
        try:
            lock()
        except OSError as exc:
            raise AppError("data_directory_in_use", status=409) from exc
        try:
            yield
        finally:
            handle.seek(0)
            unlock()

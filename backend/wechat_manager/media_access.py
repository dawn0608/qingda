"""共享媒体目录内固定64路读写锁；不持有数据库连接等待音频转换。"""

import errno
import os
import stat
from contextlib import contextmanager


@contextmanager
def history_media_access(directory, cid, *, exclusive=False):
    descriptor = os.open(
        directory / f".history-media-{int(cid) % 64}.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("invalid_media_lock")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            yield False
            return
        yield True
    finally:
        os.close(descriptor)

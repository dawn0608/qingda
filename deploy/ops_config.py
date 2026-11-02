"""运维配置原子替换；不输出配置或凭据。"""

import os
from pathlib import Path

from proxy import APP


def write(path, data, mode=0o600):
    path = Path(path)
    previous = path.stat() if path.exists() else None
    tmp = path.with_name(path.name + ".ops-tmp")
    with tmp.open("wb") as stream:
        stream.write(data if isinstance(data, bytes) else data.encode())
        stream.flush()
        os.fsync(stream.fileno())
    tmp.chmod(previous.st_mode & 0o777 if previous else mode)
    if previous:
        os.chown(tmp, previous.st_uid, previous.st_gid)
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def environment(values):
    path = APP / ".env"
    lines = path.read_text().splitlines()
    pending = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0]
        if key in pending:
            lines[index] = key + "=" + str(pending.pop(key))
    lines += [key + "=" + str(value) for key, value in pending.items()]
    write(path, "\n".join(lines) + "\n")

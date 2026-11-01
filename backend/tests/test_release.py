"""原生发布包、回调精确切换及并发发布锁验收。"""

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
release = runpy.run_path(str(ROOT / "deploy/update.py"))


def test_release_lock_rejects_overlap_and_releases_on_failure(tmp_path):
    lock = release["storage_lock"]
    path = tmp_path / "release.lock"
    with pytest.raises(ValueError):
        with lock(path):
            with pytest.raises(RuntimeError, match="storage_operation_in_progress"):
                with lock(path):
                    pytest.fail("不能取得重复发布锁")
            raise ValueError("模拟发布失败")
    with lock(path):
        pass

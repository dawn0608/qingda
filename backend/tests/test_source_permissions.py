"""服务器受限 umask 下新增源码目录的实际权限回归。"""

import importlib.util
import os
from pathlib import Path

import pytest


def updater():
    spec = importlib.util.spec_from_file_location(
        "source_update", Path(__file__).resolve().parents[2] / "deploy/update.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name == "nt", reason="服务器 POSIX 权限契约")
def test_new_source_directories_readable_under_restrictive_umask(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    old = os.umask(0o077)
    try:
        updater().ensure_source_parent(app / "prompts/greetings/new.md", app)
    finally:
        os.umask(old)
    assert (app / "prompts").stat().st_mode & 0o777 == 0o755
    assert (app / "prompts/greetings").stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name == "nt", reason="服务器 POSIX 权限契约")
def test_existing_private_directory_permissions_preserved(tmp_path):
    app = tmp_path / "app"
    secret = app / "private"
    secret.mkdir(parents=True)
    secret.chmod(0o700)
    updater().ensure_source_parent(secret / "config", app)
    assert secret.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError):
        updater().ensure_source_parent(tmp_path / "outside/config", app)

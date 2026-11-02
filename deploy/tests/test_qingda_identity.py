"""新项目身份的运行与发布边界；在服务器隔离环境验证。"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))


def test_new_module_is_runnable_without_old_alias():
    env = {**os.environ, "PYTHONPATH": str(ROOT / "backend")}
    child = subprocess.run(
        [sys.executable, "-m", "qingda", "--help"], env=env, capture_output=True, text=True
    )
    assert child.returncode == 0, child.stderr
    child = subprocess.run(
        [sys.executable, "-m", "wechat_manager", "--help"], env=env, capture_output=True, text=True
    )
    assert child.returncode != 0


def test_retired_layout_selection_is_rejected():
    env = {**os.environ, "PYTHONPATH": str(ROOT / "deploy"), "QINGDA_DEPLOY_LAYOUT": "legacy"}
    child = subprocess.run([sys.executable, "-c", "import layout"], env=env, capture_output=True)
    assert child.returncode != 0


@pytest.mark.parametrize("identity", [None, "other-project"])
def test_release_rejects_foreign_or_missing_project_identity(tmp_path, identity):
    import update

    manifest = {"revision": "test", "files": {}}
    if identity:
        manifest["project_identity"] = identity
    bundle = tmp_path / "release.tar.gz"
    body = json.dumps(manifest).encode()
    with tarfile.open(bundle, "w:gz") as archive:
        member = tarfile.TarInfo("release.json")
        member.size = len(body)
        archive.addfile(member, io.BytesIO(body))
    target = tmp_path / "candidate"
    target.mkdir()
    with pytest.raises(ValueError, match="project_identity"):
        update.extract(bundle, hashlib.sha256(bundle.read_bytes()).hexdigest(), target)
    assert list(target.iterdir()) == []


def test_release_accepts_new_identity_and_verifies_content(tmp_path):
    import update

    payload = b"new source"
    manifest = {
        "project_identity": "qingda",
        "revision": "test",
        "files": {"source.py": hashlib.sha256(payload).hexdigest()},
    }
    bundle = tmp_path / "release.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for name, body in [("release.json", json.dumps(manifest).encode()), ("source.py", payload)]:
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    target = tmp_path / "candidate"
    target.mkdir()
    update.extract(bundle, hashlib.sha256(bundle.read_bytes()).hexdigest(), target)
    assert (target / "source.py").read_bytes() == payload

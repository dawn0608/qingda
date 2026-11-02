"""从已提交的源码生成不含凭据、数据的服务器发布包。"""

import argparse
import hashlib
import json
import runpy
import subprocess
import tarfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PIP_VERSION = "25.3"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "temp/qingda-release.tar.gz")
    parser.add_argument("--baseline", type=Path, help="增量发布的已核实线上源码快照")
    parser.add_argument("--nginx-sha256", help="已核实的线上网关哈希")
    args = parser.parse_args()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip():
        raise ValueError("请先提交源码，确保发布版本可复核")
    if not (ROOT / "frontend/dist/index.html").is_file():
        raise ValueError("请先运行项目检查或构建前端")
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    staging = ROOT / "temp/release-dependencies"
    staging.mkdir(parents=True, exist_ok=True)
    requirements = staging / "requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    with urlopen(f"https://pypi.org/pypi/pip/{PIP_VERSION}/json", timeout=30) as response:
        metadata = json.load(response)
    wheel_info = next(item for item in metadata["urls"] if item["filename"].endswith(".whl"))
    with urlopen(wheel_info["url"], timeout=30) as response:
        wheel = response.read()
    if hashlib.sha256(wheel).hexdigest() != wheel_info["digests"]["sha256"]:
        raise ValueError("pip 引导 wheel 哈希不符")
    pip_file = staging / "pip.whl"
    pip_file.write_bytes(wheel)
    tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    files = {name: ROOT / name for name in tracked}
    compile_catalog = runpy.run_path(str(ROOT / "backend/wechat_manager/emotion_catalog.py"))[
        "compile_catalog"
    ]
    catalog = staging / "native_emotions.json"
    catalog.write_text(
        json.dumps(compile_catalog(ROOT), ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    files["backend/wechat_manager/native_emotions.json"] = catalog
    for path in (ROOT / "frontend/dist").rglob("*"):
        if path.is_file():
            files[path.relative_to(ROOT).as_posix()] = path
    if any(path.is_symlink() for path in files.values()):
        raise ValueError("发布源码不得包含符号链接")
    files.update({"deploy/requirements.txt": requirements, "deploy/pip.whl": pip_file})
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
    release = {"revision": revision, "files": hashes, "tracked": {name: hashes[name] for name in tracked}}
    if args.baseline:
        import base64

        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        if baseline["drift"] or not args.nginx_sha256 or len(args.nginx_sha256) != 64:
            raise ValueError("必须先核对线上源码与网关基线")
        release.update(
            expected_manifest_sha256=baseline["manifest_sha256"],
            expected_nginx_sha256=args.nginx_sha256,
            before={
                name: baseline.get("file_hashes", {}).get(name)
                or (
                    hashlib.sha256(base64.b64decode(baseline["files"][name])).hexdigest()
                    if name in baseline.get("files", {})
                    else None
                )
                for name in files
            },
        )
    manifest = staging / "release.json"
    manifest.write_text(
        json.dumps(
            release,
            indent=2,
        ),
        encoding="utf-8",
    )
    files["release.json"] = manifest
    with tarfile.open(args.output, "w:gz") as archive:
        for name, path in files.items():
            archive.add(path, arcname=name)
    print(
        json.dumps(
            {
                "bundle": str(args.output),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                "revision": revision,
            }
        )
    )


if __name__ == "__main__":
    main()

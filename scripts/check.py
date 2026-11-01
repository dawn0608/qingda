"""仅服务器隔离回归；必须显式指定专用 PostgreSQL 测试库。"""

import os
import subprocess
import sys
from pathlib import Path

if os.name == "nt" or "test" not in os.environ.get("TEST_DATABASE_URL", "").lower():
    raise RuntimeError("只能在服务器指定TEST_DATABASE_URL隔离库执行，禁止本地或生产库验收")

root = Path(__file__).resolve().parents[1]
for command, directory in [
    ([sys.executable, "-m", "ruff", "check", "backend", "deploy", "scripts"], root),
    ([sys.executable, "-m", "ruff", "format", "--check", "backend", "deploy", "scripts"], root),
    ([sys.executable, "-m", "pytest", "-q"], root),
    (
        [
            "node",
            "--test",
            *map(str, (root / "frontend/tests").glob("*.test.mjs")),
            *map(str, (root / "frontend/src").glob("*.test.mjs")),
        ],
        root,
    ),
    (["npm.cmd" if os.name == "nt" else "npm", "run", "build"], root / "frontend"),
]:
    subprocess.run(command, cwd=directory, check=True)

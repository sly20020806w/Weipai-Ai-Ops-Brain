"""只接受验收脚本创建的专用本地临时库。"""

import os
import subprocess
import sys
from pathlib import Path

from app.config import parse_database_url


def get_test_database_url() -> str:
    value = os.environ["TEST_DATABASE_URL"]
    url = parse_database_url(value)
    if url.host not in {"127.0.0.1", "localhost", "::1"} or not (
        url.database and url.database.startswith("weipai_db_test_")
    ):
        raise ValueError("数据库验收只允许本机专用临时测试库")
    return value


def migrate(*arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ, APP_ENV="test", DATABASE_URL=get_test_database_url()),
        check=True,
    )

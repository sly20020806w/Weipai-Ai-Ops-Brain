"""离线导出 API 契约；不读取部署配置、不启动数据库或外部客户端。"""

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

from app.api.main import create_app
from app.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="导出可重复生成的前端 OpenAPI 契约")
    parser.add_argument("output", type=Path)
    options = parser.parse_args()
    with patch.dict(os.environ, {}, clear=True):
        schema = create_app(Settings(APP_ENV="test")).openapi()
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(
        json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()

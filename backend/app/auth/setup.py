"""辅助脚本只把新配置交还给父进程，永不写文件；密码不出现在命令行。"""

import argparse
import json
import os
import secrets

from app.auth.config import AuthConfig
from app.auth.passwords import hash_password


def main() -> None:
    parser = argparse.ArgumentParser(description="生成单用户登录环境配置")
    parser.add_argument("--username", required=True)
    parser.add_argument("--origin", required=True)
    options = parser.parse_args()
    values = {
        "username": options.username,
        "password_hash": hash_password(os.environ["WEIPAI_AUTH_SETUP_PASSWORD"]),
        "session_secret": secrets.token_hex(32),
        "public_origin": options.origin,
    }
    AuthConfig.model_validate(values)
    # 调用者必须捕获 stdout 到 AUTH_CONFIG；不能把它打印到终端或日志。
    print(json.dumps(values, ensure_ascii=True))


if __name__ == "__main__":
    main()

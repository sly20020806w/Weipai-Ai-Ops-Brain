"""启动既有本机 Fake 演示；登录凭证只保留在进程内，停止标记触发原有清理。"""

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from app.config import Settings
import check_db
import demo_frontend

state_path = ROOT / ".cache/live-demo-status.json"
stop_path = ROOT / ".cache/live-demo-stop"
password = os.environ.pop("WEIPAI_LIVE_DEMO_PASSWORD", "")
if not 12 <= len(password) <= 256:
    raise ValueError("本机演示需要12–256字符的临时密码")
if stop_path.exists():
    raise ValueError("存在上次停止标记，请先核对服务状态")

for field in Settings.model_fields.values():
    if isinstance(field.validation_alias, str):
        os.environ.pop(field.validation_alias, None)
os.environ.update(APP_ENV="test", CONNECTOR_MODE="fake", LLM_MODE="fake")
state = {"status": "starting", "pid": os.getpid(), "scope": "local_fake",
         "started_at": datetime.now(UTC).isoformat(), "username": "local-browser-owner"}

def save_state():
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

save_state()
ports = []
original_port = demo_frontend.free_port

def capture_port():
    port = original_port()
    ports.append(port)
    return port

def keep_serving():
    if len(ports) != 2:
        raise RuntimeError("不能确定本机 API 和前端端口")
    state.update(status="ready", api_origin=f"http://127.0.0.1:{ports[0]}",
                 frontend_origin=f"http://127.0.0.1:{ports[1]}",
                 url=f"http://127.0.0.1:{ports[1]}/chat?service=payment-service&start=2026-10-01T01:00:00Z&end=2026-10-01T02:00:00Z")
    save_state()
    print("演示已在后台保持运行；停止标记会关闭服务并清理临时数据库。", flush=True)
    while not stop_path.exists():
        time.sleep(1)

sys.argv = [str(ROOT / "scripts/check_db.py"), "--chat-pages", "--interactive"]
try:
    with patch("getpass.getpass", return_value=password), \
         patch.object(demo_frontend, "free_port", capture_port), \
         patch.object(demo_frontend, "wait_for_close", keep_serving):
        check_db.main()
    state["status"] = "stopped"
except BaseException as error:
    state.update(status="failed", error_type=type(error).__name__)
    raise
finally:
    state["finished_at"] = datetime.now(UTC).isoformat()
    save_state()

"""保存本次本地影子验收证据；失败也覆盖最新状态，避免展示旧成功。"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from app.db.base import utc_now
from app.learning.evaluation.acceptance import AcceptanceCriteria

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 58 本地全 Fake 影子验收")
    parser.add_argument("--rca-threshold", type=float, default=1.0)
    parser.add_argument("--minimum-samples", type=int, default=3)
    args = parser.parse_args()
    criteria = AcceptanceCriteria(
        rca_hit_rate=args.rca_threshold, minimum_samples=args.minimum_samples
    )
    directory = ROOT / ".cache" / "shadow" / uuid4().hex
    directory.mkdir(parents=True)
    latest = directory.parent / "latest.json"
    result: dict[str, object] = {
        "scope": "local_fake",
        "status": "running",
        "started_at": utc_now().isoformat(),
        "report_directory": str(directory),
        "criteria": criteria.model_dump(mode="json"),
        "real_environment_verified": False,
        "production_writes_enabled": False,
    }
    latest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    environment = dict(
        os.environ,
        STEP58_REPORT_DIRECTORY=str(directory),
        STEP58_RCA_THRESHOLD=str(criteria.rca_hit_rate),
        STEP58_MINIMUM_SAMPLES=str(criteria.minimum_samples),
    )
    try:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/check_db.py"), "--shadow"],
            cwd=ROOT / "backend",
            env=environment,
            check=True,
        )
        files = [directory / f"shadow-{n}.json" for n in range(1, 4)] + [
            directory / "benchmark.json"
        ]
        result["evidence"] = [json.loads(p.read_text(encoding="utf-8")) for p in files]
        result["status"] = "passed"
    except BaseException:
        result["status"] = "failed"
        raise
    finally:
        result["finished_at"] = utc_now().isoformat()
        latest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"本次影子验收报告：{latest}", flush=True)


if __name__ == "__main__":
    main()

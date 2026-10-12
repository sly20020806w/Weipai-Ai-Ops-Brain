import hashlib
import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
report_path = root / '.cache/acceptance/adf0f7933f89496087a9112e2dd82380/report.json'
report = json.loads(report_path.read_text(encoding='utf-8'))
names = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=root, check=True, capture_output=True).stdout.decode('utf-8').split('\0')
files = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sorted(set(names) - {''}) if (root / name).is_file()}
digest = hashlib.sha256()
for name, value in files.items():
    digest.update(name.encode('utf-8') + b'\0')
    digest.update(bytes.fromhex(value))
assert digest.hexdigest() == report['source_sha256'], '源码已变化，不能补充旧检查快照'
report['source_files'] = files
for stage in report['stages']:
    path = Path(stage['log'])
    stage['log_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
encoded = json.dumps(report, ensure_ascii=False, indent=2)
report_path.write_text(encoded, encoding='utf-8')
(report_path.parent.parent / 'latest.json').write_text(encoded, encoding='utf-8')
print('已核对源码指纹并补充检查快照及日志哈希；原失败状态保留')

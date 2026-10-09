"""Linux CI 使用固定官方扫描工具；Windows 保留既有安装入口。"""

import hashlib
import platform
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path


def verified_download(url: str, target: Path, expected: str) -> None:
    if not target.is_file():
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = response.read()
        if hashlib.sha256(payload).hexdigest() != expected:
            raise RuntimeError("安全工具下载 SHA256 校验失败")
        target.write_bytes(payload)
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        raise RuntimeError("安全工具缓存 SHA256 校验失败；不能继续扫描")


def prepare_tools(root: Path) -> tuple[Path, Path]:
    tools = root / ".tools" / "security"
    if sys.platform == "win32":
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-File",
                str(root / "scripts/install-security-tools.ps1"),
            ],
            cwd=root,
            capture_output=True,
            timeout=180,
        )
        if result.returncode:
            raise RuntimeError("扫描工具安装/校验失败；未将扫描标记为通过")
        return tools / "gitleaks-8.30.1/gitleaks.exe", tools / "osv-scanner-2.6.0.exe"
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "AMD64"}:
        raise ValueError("安全扫描只支持经过校验的 Windows/Linux x64 工具")
    tools = tools / "linux-x64"
    tools.mkdir(parents=True, exist_ok=True)
    archive = tools / "gitleaks-8.30.1.tar.gz"
    verified_download(
        "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/"
        "gitleaks_8.30.1_linux_x64.tar.gz",
        archive,
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    )
    leaks = tools / "gitleaks-8.30.1"
    # 只读出指定普通文件，绝不把压缩包路径解压到文件系统。
    with tarfile.open(archive) as source:
        member = source.getmember("gitleaks")
        if not member.isfile() or member.size > 100_000_000:
            raise RuntimeError("Gitleaks 官方压缩包缺少有效二进制")
        stream = source.extractfile(member)
        if stream is None:
            raise RuntimeError("Gitleaks 二进制读取失败")
        with stream:
            payload = stream.read()
    if leaks.is_file() and leaks.read_bytes() != payload:
        raise RuntimeError("Gitleaks 二进制缓存被修改；不能继续扫描")
    leaks.write_bytes(payload)
    leaks.chmod(0o755)
    osv = tools / "osv-scanner-2.6.0"
    verified_download(
        "https://github.com/google/osv-scanner/releases/download/v2.6.0/osv-scanner_linux_amd64",
        osv,
        "ca69b3d3cd08f889a49dc0a383122f71cc528b83803671df5fd874d97485b108",
    )
    osv.chmod(0o755)
    return leaks, osv

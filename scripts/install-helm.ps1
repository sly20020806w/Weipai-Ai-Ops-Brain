# 仅安装固定版本工具到仓库忽略目录；不修改系统 PATH 或 Kubernetes。
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$helmDir = Join-Path $projectRoot '.tools\helm'
$helmExe = Join-Path $helmDir 'windows-amd64\helm.exe'
$expectedArchiveHash = '6488630c2e5d5945ed990fa02fd9e99f9c6792cdbcd79eb264b6cfb90179d2d1'
$archivePath = Join-Path $helmDir 'helm-v3.19.0-windows-amd64.zip'
New-Item -ItemType Directory -Path $helmDir -Force | Out-Null
if (-not (Test-Path -LiteralPath $archivePath)) {
    Invoke-WebRequest -UseBasicParsing -Uri 'https://get.helm.sh/helm-v3.19.0-windows-amd64.zip' -OutFile $archivePath
}
if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedArchiveHash) {
    throw 'Helm 归档校验失败；拒绝运行，请移除损坏的 .tools\helm 缓存后重试。'
}
Expand-Archive -LiteralPath $archivePath -DestinationPath $helmDir -Force
& $helmExe version --short
if ($LASTEXITCODE -ne 0) { throw 'Helm 无法启动。' }

# 仅下载到仓库忽略目录；固定官方发布、校验 SHA256，不修改系统 PATH。
$ErrorActionPreference = 'Stop'
# uv/其他宿主可能继承 PowerShell 7 的模块路径，显式恢复当前 5.1 系统模块。
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + $env:PSModulePath
$projectRoot = Split-Path -Parent $PSScriptRoot
$securityTools = Join-Path $projectRoot '.tools\security'
New-Item -ItemType Directory -Force -Path $securityTools | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Receive-VerifiedTool([string]$Url, [string]$Path, [string]$Sha256) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Path
    }
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Sha256) {
        throw "安全工具 SHA256 校验失败：$Path；请移走该文件后重新运行。"
    }
}

$archive = Join-Path $securityTools 'gitleaks.zip'
Receive-VerifiedTool 'https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_windows_x64.zip' $archive 'd29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e'
Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $securityTools 'gitleaks-8.30.1') -Force
Receive-VerifiedTool 'https://github.com/google/osv-scanner/releases/download/v2.6.0/osv-scanner_windows_amd64.exe' (Join-Path $securityTools 'osv-scanner-2.6.0.exe') 'e0ed7644118b717b028c249ee9d3515024e55e8510747ca08906eb96765354d6'
Write-Host '已准备 Gitleaks 8.30.1 与 OSV-Scanner 2.6.0（官方发布 SHA256 校验通过）。'

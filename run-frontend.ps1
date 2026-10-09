# 本机控制台：同源代理到 127.0.0.1:8000；后端 Origin 要设为 http://127.0.0.1:5173。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$projectPnpm = Get-ProjectPnpm
& $projectPnpm --dir (Join-Path $PSScriptRoot 'frontend') run dev
exit $LASTEXITCODE

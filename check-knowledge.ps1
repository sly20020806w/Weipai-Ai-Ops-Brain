# Fake embedding + 本项目独立本地 pgvector 临时库；退出时自动清理。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/check_db.py --knowledge
exit $LASTEXITCODE

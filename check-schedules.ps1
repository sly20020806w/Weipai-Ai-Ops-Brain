# 实际本机 Temporal Schedules、官方时间跳跃工具与自动清理的临时数据库。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/check_db.py --schedules
exit $LASTEXITCODE

# 独立临时库与隔离队列；周期即时触发，发布 Timer 仅在演示配置中缩短为 2 秒。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/check_db.py --schedules-demo
exit $LASTEXITCODE

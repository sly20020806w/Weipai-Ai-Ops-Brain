# 独立临时库、隔离 Temporal Schedule/队列，展示异常、去重与健康静默。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/check_db.py --detection-demo
exit $LASTEXITCODE

# Step 39：正常发布、异常暂停/回滚及高风险 SQL 演示。
param([switch]$Interactive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$projectUv = Get-ProjectUv
$demoArguments = @('--releases-demo')
if ($Interactive) { $demoArguments += '--interactive' }
& $projectUv run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/check_db.py @demoArguments
exit $LASTEXITCODE

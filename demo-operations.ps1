# Step 45：自动启动临时 API 和 Fake 巡检 Worker，演示结束后清理。
param([switch]$Interactive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$projectUv = Get-ProjectUv
$demoArguments = @('run', '--frozen', '--directory', (Join-Path $PSScriptRoot 'backend'), 'python', '../scripts/check_db.py', '--operations-demo')
if ($Interactive) { $demoArguments += '--interactive' }
& $projectUv @demoArguments
exit $LASTEXITCODE

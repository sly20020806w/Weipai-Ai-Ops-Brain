# Step 29：问题卡片、回答信号、任务恢复与草稿演示，可自行输入回答。
param([switch]$Interactive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$projectUv = Get-ProjectUv
$demoArguments = @('run', '--frozen', '--directory', (Join-Path $PSScriptRoot 'backend'), 'python', '../scripts/check_db.py', '--human-demo')
if ($Interactive) { $demoArguments += '--interactive' }
& $projectUv @demoArguments
exit $LASTEXITCODE

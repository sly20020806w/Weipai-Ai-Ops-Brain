param([switch]$Interactive)
# Step 42：Fake 活动检查、异常派发、分别审批准备与回收资源。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$projectUv = Get-ProjectUv
$demoArguments = @('run', '--frozen', '--directory', (Join-Path $PSScriptRoot 'backend'), 'python', '../scripts/check_db.py', '--war-room-demo')
if ($Interactive) { $demoArguments += '--interactive' }
& $projectUv @demoArguments
exit $LASTEXITCODE

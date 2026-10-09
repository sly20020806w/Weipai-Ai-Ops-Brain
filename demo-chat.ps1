# Step 46：自动启动临时 API/Worker，逐帧显示真实 SSE。
param([switch]$Interactive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$projectUv = Get-ProjectUv
$demoArguments = @('run', '--frozen', '--directory', (Join-Path $PSScriptRoot 'backend'), 'python', '../scripts/check_db.py', '--chat-demo')
if ($Interactive) { $demoArguments += '--interactive' }
& $projectUv @demoArguments
exit $LASTEXITCODE

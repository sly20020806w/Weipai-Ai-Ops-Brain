# Step 44：真实本机 HTTP 查询、审批、问答与接管；自动清理隔离数据。
param([switch]$Interactive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$projectUv = Get-ProjectUv
$demoArguments = @('run', '--frozen', '--directory', (Join-Path $PSScriptRoot 'backend'),
    'python', '../scripts/check_db.py', '--console-demo')
if ($Interactive) { $demoArguments += '--interactive' }
& $projectUv @demoArguments
exit $LASTEXITCODE

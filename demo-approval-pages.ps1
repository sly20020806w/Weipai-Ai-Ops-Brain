# Step 49：真实浏览器、API 与 Temporal 的 Fake 人工处理验收。
param([switch]$Interactive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$projectUv = Get-ProjectUv
$projectPnpm = Get-ProjectPnpm
& $projectPnpm --dir (Join-Path $PSScriptRoot 'frontend') run build
if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
$demoArguments = @('run', '--frozen', '--directory', (Join-Path $PSScriptRoot 'backend'), 'python', '../scripts/check_db.py', '--approval-pages')
if ($Interactive) { $demoArguments += '--interactive' }
& $projectUv @demoArguments
exit $LASTEXITCODE

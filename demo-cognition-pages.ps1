# Step 50：本机临时数据库、真实 API 与 Edge 的 Fake 认知页面验收。
param([switch]$Interactive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$projectUv = Get-ProjectUv
$projectPnpm = Get-ProjectPnpm
& $projectPnpm --dir (Join-Path $PSScriptRoot 'frontend') run build
if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
$demoArguments = @('run', '--frozen', '--directory', (Join-Path $PSScriptRoot 'backend'), 'python', '../scripts/check_db.py', '--cognition-pages')
if ($Interactive) { $demoArguments += '--interactive' }
& $projectUv @demoArguments
exit $LASTEXITCODE

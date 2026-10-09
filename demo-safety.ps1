# Step 33：逐项展示熔断证据、动作拦截与 Fake 接管通知。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
$projectUv = Get-ProjectUv
& $projectUv run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/check_db.py --safety-demo
exit $LASTEXITCODE

# Step 31：指标恢复／未恢复与外部代码设置 RESOLVED 被拒绝的 Fake 演示。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$projectUv = Get-ProjectUv
& $projectUv run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/check_db.py --verifier-demo
exit $LASTEXITCODE

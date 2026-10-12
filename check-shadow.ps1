# Step 58 本地影子演练与 Replay 门槛；真实环境仍待入职后验收。
param([double]$RcaThreshold = 1.0, [int]$MinimumSamples = 3)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$projectUv = Get-ProjectUv
& $projectUv run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/check_shadow.py --rca-threshold $RcaThreshold --minimum-samples $MinimumSamples
exit $LASTEXITCODE

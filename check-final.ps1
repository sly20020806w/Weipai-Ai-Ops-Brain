# Step 58：平台本地最终验收，不连接真实环境、不上线 ACK。
param([double]$RcaThreshold = 1.0, [int]$MinimumSamples = 3,
      [string]$LocalKindNode = 'weipai-sim-control-plane', [string]$ResumeFrom)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$projectUv = Get-ProjectUv
$finalArguments = @('--rca-threshold', $RcaThreshold, '--minimum-samples', $MinimumSamples, '--local-kind-node', $LocalKindNode)
if ($ResumeFrom) { $finalArguments += @('--resume-from', (Resolve-Path -LiteralPath $ResumeFrom).Path) }
& $projectUv run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/check_final.py @finalArguments
exit $LASTEXITCODE

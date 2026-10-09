# Step 55：扫描、全部接口鉴权、本机隔离 RBAC 和现有鉴权集成回归。
param([string]$LocalKindNode = 'weipai-sim-control-plane')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$projectUv = Get-ProjectUv
& $projectUv run --frozen --directory (Join-Path $PSScriptRoot 'backend') python -m pytest tests/test_security.py -v --tb=short --basetemp (Join-Path $PSScriptRoot '.cache\pytest-security')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $projectUv run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/security_checks.py --local-kind-node $LocalKindNode
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& powershell.exe -NoProfile -File (Join-Path $PSScriptRoot 'check-auth.ps1')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host 'Step 55 安全验收全部通过。'

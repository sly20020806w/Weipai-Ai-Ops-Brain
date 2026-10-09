# Step 56：Docker 多阶段构建与两个后端入口的本机验收。
param([switch]$SkipBuild, [switch]$Interactive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$projectUv = Get-ProjectUv
$arguments = @()
if ($SkipBuild) { $arguments += '--skip-build' }
if ($Interactive) { $arguments += '--interactive' }
& $projectUv run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/check_images.py @arguments
exit $LASTEXITCODE

# Step 45：认知与运营 API，使用本机独立临时库及 Fake。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
$projectUv = Get-ProjectUv
& $projectUv run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/check_db.py --operations
exit $LASTEXITCODE

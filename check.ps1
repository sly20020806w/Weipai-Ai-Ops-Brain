$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/check.py
exit $LASTEXITCODE

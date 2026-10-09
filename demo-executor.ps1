param([switch]$Interactive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$projectUv = Get-ProjectUv
$demoArguments = @('../scripts/check_db.py', '--executor-demo')
if ($Interactive) { $demoArguments += '--interactive' }
& $projectUv run --frozen --directory (Join-Path $PSScriptRoot 'backend') python @demoArguments
exit $LASTEXITCODE

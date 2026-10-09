param([ValidateSet('once', 'schedule', 'pause', 'resume')][string]$Mode = 'once')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-db.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$env:CONNECTOR_MODE = 'fake'
$env:LLM_MODE = 'fake'
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory (Join-Path $PSScriptRoot 'backend') python -m app.graph.discovery.demo $Mode
exit $LASTEXITCODE

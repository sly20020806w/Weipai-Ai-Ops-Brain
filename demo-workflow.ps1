param(
    [ValidateSet('normal', 'signal', 'timeout')]
    [string]$Mode = 'normal'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-db.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$env:CONNECTOR_MODE = 'fake'
$env:LLM_MODE = 'fake'
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory (Join-Path $PSScriptRoot 'backend') python -m app.tasks.demo $Mode
exit $LASTEXITCODE

# 自动升级本项目本机应用库，然后经正在运行的 Worker 验收固定 Fake 历史。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
. (Join-Path $PSScriptRoot 'use-local-db.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
$env:CONNECTOR_MODE = 'fake'
$env:LLM_MODE = 'fake'
$uvPath = Get-ProjectUv
& $uvPath run --frozen --directory (Join-Path $PSScriptRoot 'backend') alembic upgrade head
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $uvPath run --frozen --directory (Join-Path $PSScriptRoot 'backend') python -m app.graph.changes.demo
exit $LASTEXITCODE

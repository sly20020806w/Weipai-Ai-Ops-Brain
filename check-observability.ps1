# Step 13 离线专项验收，不需要 Prometheus/SLS/ARMS 凭证、Docker 或数据库。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$taskUv = Get-ProjectUv
& $taskUv run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    pytest -vv --basetemp (Join-Path $PSScriptRoot '.cache\pytest-observability') `
    tests/test_observability.py tests/test_observability_tools.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $taskUv run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    python -m app.connectors.observability
exit $LASTEXITCODE

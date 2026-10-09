# Step 10 离线专项验收，不需要公司凭证、Docker 或数据库。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$uvPath = Get-ProjectUv
& $uvPath run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    pytest -vv --basetemp (Join-Path $PSScriptRoot '.cache\pytest-connectors') `
    tests/test_connectors.py tests/test_connector_boundaries.py
exit $LASTEXITCODE

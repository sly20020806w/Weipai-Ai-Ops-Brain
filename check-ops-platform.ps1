# Step 11 离线专项验收，不需要公司凭证、Docker 或数据库。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$uvPath = Get-ProjectUv
& $uvPath run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    pytest -vv --basetemp (Join-Path $PSScriptRoot '.cache\pytest-ops-platform') `
    tests/test_ops_platform.py tests/test_ops_platform_tools.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $uvPath run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    python -m app.connectors.ops_platform
exit $LASTEXITCODE

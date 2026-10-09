# Step 9 离线专项验收，不需要网关凭证、Docker 或数据库。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$uvPath = Get-ProjectUv
& $uvPath run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    pytest -vv tests/test_tools.py
exit $LASTEXITCODE

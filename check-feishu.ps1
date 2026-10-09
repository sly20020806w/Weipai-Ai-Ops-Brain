# Step 16 离线验收，不需要飞书凭证、Docker 或数据库。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$taskUv = Get-ProjectUv
& $taskUv run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    pytest -vv --basetemp (Join-Path $PSScriptRoot '.cache\pytest-feishu') `
    tests/test_feishu.py tests/test_feishu_tools_boundary.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $taskUv run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    python -m app.connectors.feishu
exit $LASTEXITCODE

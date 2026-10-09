# Step 15 离线验收，不需要阿里云凭证、Docker 或数据库。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$taskUv = Get-ProjectUv
& $taskUv run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    pytest -vv --basetemp (Join-Path $PSScriptRoot '.cache\pytest-cloud') `
    tests/test_cloud.py tests/test_cloud_tools.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $taskUv run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    python -m app.connectors.cloud
exit $LASTEXITCODE

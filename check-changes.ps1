# Step 14 离线验收，不需要 Git/CI/ArgoCD/配置中心凭证、Docker 或数据库。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$taskUv = Get-ProjectUv
& $taskUv run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    pytest -vv --basetemp (Join-Path $PSScriptRoot '.cache\pytest-changes') `
    tests/test_changes.py tests/test_changes_tools.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $taskUv run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    python -m app.connectors.changes
exit $LASTEXITCODE

# Step 12 离线专项验收，不需要 Kubernetes 凭证、Docker 或数据库。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$uvPath = Get-ProjectUv
& $uvPath run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    pytest -vv --basetemp (Join-Path $PSScriptRoot '.cache\pytest-kubernetes') `
    tests/test_kubernetes.py tests/test_kubernetes_tools.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $uvPath run --offline --frozen --directory (Join-Path $PSScriptRoot 'backend') `
    python -m app.connectors.kubernetes
exit $LASTEXITCODE

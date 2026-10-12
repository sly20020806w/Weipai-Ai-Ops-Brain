# 本地 Step 57 验收：Helm 渲染 + 隔离数据库/Temporal + Fake 来源，不部署 ACK。
param([string]$ValuesFile)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$uvPath = Get-ProjectUv
if (-not (Get-Command helm -ErrorAction SilentlyContinue)) {
    & (Join-Path $PSScriptRoot 'scripts\install-helm.ps1')
}
$helmArguments = @('../scripts/check_helm.py')
if ($ValuesFile) {
    $resolvedValuesFile = (Resolve-Path -LiteralPath $ValuesFile).Path
    $helmArguments += @('--values', $resolvedValuesFile)
}
& $uvPath run --frozen --directory (Join-Path $PSScriptRoot 'backend') python @helmArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
. (Join-Path $PSScriptRoot 'use-local-temporal.ps1')
& $uvPath run --frozen --directory (Join-Path $PSScriptRoot 'backend') python ../scripts/check_db.py --ack
exit $LASTEXITCODE

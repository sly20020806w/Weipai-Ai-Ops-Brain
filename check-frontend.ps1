# Step 47：离线 OpenAPI 一致性、lint、严格类型、组件测试与生产构建。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$projectPnpm = Get-ProjectPnpm
$projectUv = Get-ProjectUv
$previousUv = $env:WEIPAI_UV
try {
    $env:WEIPAI_UV = $projectUv
    foreach ($command in @('api:check', 'lint', 'typecheck', 'test', 'build')) {
        & $projectPnpm --dir (Join-Path $PSScriptRoot 'frontend') run $command
        if ($LASTEXITCODE -ne 0) { throw "前端检查失败：$command。" }
    }
    Write-Host '前端检查全部通过。'
} finally {
    [Environment]::SetEnvironmentVariable('WEIPAI_UV', $previousUv, 'Process')
}

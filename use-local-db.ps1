# 复用本项目本地容器配置，为 API/Alembic 设置当前进程环境。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'use-local-deps.ps1')
$containerIds = @(& docker ps --all --quiet `
    --filter 'label=com.docker.compose.project=weipai-ai-ops-brain-local' `
    --filter 'label=com.docker.compose.service=postgres')
if ($LASTEXITCODE -ne 0 -or $containerIds.Count -ne 1) {
    throw '无法定位本项目的本地 PostgreSQL 容器。'
}
$containerData = (& docker inspect $containerIds[0] | ConvertFrom-Json)[0]
if ($LASTEXITCODE -ne 0) { throw '无法读取本地数据库端口。' }
$portBindings = @($containerData.NetworkSettings.Ports.'5432/tcp')
if ($portBindings.Count -ne 1 -or $portBindings[0].HostIp -ne '127.0.0.1') {
    throw '要求本地数据库仅绑定 127.0.0.1；请先启动本地依赖容器。'
}
$encodedUser = [Uri]::EscapeDataString($env:POSTGRES_USER)
$encodedPassword = [Uri]::EscapeDataString($env:POSTGRES_PASSWORD)
$databasePort = $portBindings[0].HostPort
$env:APP_ENV = 'local'
$env:DATABASE_URL = "postgresql+asyncpg://${encodedUser}:${encodedPassword}@127.0.0.1:${databasePort}/weipai"
Write-Host '已设置 APP_ENV=local 和 DATABASE_URL（仅当前进程，不显示凭证、不写文件）。'

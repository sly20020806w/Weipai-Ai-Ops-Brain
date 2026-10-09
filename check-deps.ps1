$ErrorActionPreference = 'Stop'
$composePath = Join-Path $PSScriptRoot 'deploy\docker-compose.yml'

function Invoke-LocalCompose {
    param([string[]]$DockerArguments)
    & docker compose -f $composePath @DockerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "本地依赖检查失败：docker compose $($DockerArguments -join ' ')"
    }
}

if (-not $env:POSTGRES_PASSWORD) {
    throw '请先设置 POSTGRES_PASSWORD，使用首次启动本地数据库时的同一个密码。'
}

Invoke-LocalCompose -DockerArguments @('config', '--quiet')
$containerLines = Invoke-LocalCompose -DockerArguments @('ps', '--all', '--format', 'json')
$containers = @($containerLines | ForEach-Object { $_ | ConvertFrom-Json })
foreach ($service in @('postgres', 'temporal', 'temporal-admin', 'temporal-ui')) {
    $container = @($containers | Where-Object { $_.Service -eq $service })
    if ($container.Count -ne 1 -or $container[0].State -ne 'running' -or $container[0].Health -ne 'healthy') {
        throw "$service 未达到 running/healthy，请先执行 docker compose -f deploy/docker-compose.yml up -d --wait。"
    }
    Write-Host "${service}：healthy"
}
$schema = @($containers | Where-Object { $_.Service -eq 'temporal-schema' })
if ($schema.Count -ne 1 -or $schema[0].State -ne 'exited' -or $schema[0].ExitCode -ne 0) {
    throw 'Temporal 数据库初始化未成功完成。'
}
Write-Host 'temporal-schema：已成功完成（一次性初始化容器）'

$postgresUser = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { 'weipai' }
Invoke-LocalCompose -DockerArguments @(
    'exec', '-T', 'postgres', 'psql', '-v', 'ON_ERROR_STOP=1', '-U', $postgresUser, '-d', 'weipai',
    '-c', 'CREATE EXTENSION IF NOT EXISTS vector;',
    '-c', "SELECT extversion FROM pg_extension WHERE extname = 'vector';",
    '-c', "SELECT '[1,2,3]'::vector;"
)
$databaseTimezone = Invoke-LocalCompose -DockerArguments @(
    'exec', '-T', 'postgres', 'psql', '-v', 'ON_ERROR_STOP=1', '-U', $postgresUser, '-d', 'weipai',
    '-t', '-A', '-c', 'SHOW timezone;'
)
if (($databaseTimezone -join "`n").Trim() -ne 'UTC') {
    throw '本地 PostgreSQL 时区必须为 UTC。'
}
Write-Host 'PostgreSQL：vector 扩展和向量类型可用，时区为 UTC'
Invoke-LocalCompose -DockerArguments @(
    'exec', '-T', 'temporal-admin', 'temporal', 'operator', 'cluster', 'health',
    '--address', 'temporal:7233'
)
Invoke-LocalCompose -DockerArguments @(
    'exec', '-T', 'temporal-admin', 'temporal', 'operator', 'namespace', 'describe',
    '--address', 'temporal:7233', '--namespace', 'default'
)

$uiPort = if ($env:TEMPORAL_UI_PORT) { $env:TEMPORAL_UI_PORT } else { '8080' }
$uiUrl = "http://127.0.0.1:$uiPort"
foreach ($path in @('/', '/api/v1/namespaces')) {
    $response = Invoke-WebRequest -Uri "$uiUrl$path" -UseBasicParsing -TimeoutSec 15
    if ($response.StatusCode -ne 200) {
        throw "Temporal UI 请求失败：$path"
    }
    if ($path -eq '/api/v1/namespaces') {
        $namespaceData = $response.Content | ConvertFrom-Json
        if ('default' -notin @($namespaceData.namespaces | ForEach-Object { $_.namespaceInfo.name })) {
            throw 'Temporal UI 未能从服务端读取 default 命名空间。'
        }
    }
    Write-Host "Temporal UI ${path}：HTTP 200"
}
Write-Host "本地依赖检查全部通过。浏览器打开 $uiUrl 查看 Temporal UI。"

# 将本项目已有本地 PostgreSQL 容器的配置加载到当前进程环境，不写配置文件。
$ErrorActionPreference = 'Stop'
$containerIds = @(& docker ps --all --quiet `
    --filter 'label=com.docker.compose.project=weipai-ai-ops-brain-local' `
    --filter 'label=com.docker.compose.service=postgres')
if ($LASTEXITCODE -ne 0) {
    throw '无法连接 Docker，请先启动本机 Linux 引擎。'
}
if ($containerIds.Count -ne 1) {
    throw '未找到唯一的本项目 PostgreSQL 容器；首次启动请按 deploy/README.md 设置环境变量。'
}
$containerConfig = (& docker inspect $containerIds[0] | ConvertFrom-Json)[0]
if ($LASTEXITCODE -ne 0) {
    throw '无法读取本项目 PostgreSQL 容器配置。'
}
foreach ($name in @('POSTGRES_USER', 'POSTGRES_PASSWORD')) {
    $prefix = "$name="
    $entry = @($containerConfig.Config.Env | Where-Object { $_.StartsWith($prefix) })
    if ($entry.Count -ne 1 -or $entry[0].Length -eq $prefix.Length) {
        throw "容器配置缺少 $name。"
    }
    [Environment]::SetEnvironmentVariable($name, $entry[0].Substring($prefix.Length), 'Process')
}
Write-Host '已将本项目现有数据库配置加载到当前进程环境（不显示密码、不写文件）。'

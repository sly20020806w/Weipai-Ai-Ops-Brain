# 读取本项目已有 Temporal 容器的回环端口，仅设置当前进程环境。
$ErrorActionPreference = 'Stop'
$temporalIds = @(& docker ps --quiet `
    --filter 'label=com.docker.compose.project=weipai-ai-ops-brain-local' `
    --filter 'label=com.docker.compose.service=temporal')
if ($LASTEXITCODE -ne 0 -or $temporalIds.Count -ne 1) {
    throw '请先启动本项目的本地 Temporal 容器。'
}
$temporalData = (& docker inspect $temporalIds[0] | ConvertFrom-Json)[0]
if ($LASTEXITCODE -ne 0) { throw '无法读取本地 Temporal 配置。' }
$temporalBindings = @($temporalData.NetworkSettings.Ports.'7233/tcp')
if ($temporalBindings.Count -ne 1 -or $temporalBindings[0].HostIp -ne '127.0.0.1') {
    throw 'Temporal 验收只允许本机 127.0.0.1 端口。'
}
$localTemporalAddress = "127.0.0.1:$($temporalBindings[0].HostPort)"
$env:TEMPORAL_CONFIG = @{
    address = $localTemporalAddress
    namespace = 'default'
    task_queue = 'weipai-ai-tasks'
} | ConvertTo-Json -Compress
$env:TEST_TEMPORAL_ADDRESS = $localTemporalAddress
$env:TEST_TEMPORAL_NAMESPACE = 'default'
Write-Host "已设置本地 Temporal 地址 $localTemporalAddress（仅当前进程）。"

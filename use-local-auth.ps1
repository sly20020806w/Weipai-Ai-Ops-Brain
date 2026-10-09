# 本机 API 账户：只设置当前进程 AUTH_CONFIG，不写文件、不输出密码或密钥。
param(
    [string]$Username = 'owner',
    [string]$Origin = 'http://127.0.0.1:8000'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\project.ps1')
$projectUv = Get-ProjectUv
$taskPassword = Read-Host '设置登录密码（12-256 字符）' -AsSecureString
$taskPasswordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskPassword)
try {
    $env:WEIPAI_AUTH_SETUP_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskPasswordPointer)
    $taskAuthJson = & $projectUv run --frozen --directory (Join-Path $PSScriptRoot 'backend') python -m app.auth.setup --username $Username --origin $Origin
    if ($LASTEXITCODE -ne 0) { throw '登录配置生成失败，请检查密码长度、账户名和 Origin。' }
    $null = $taskAuthJson | ConvertFrom-Json
    $env:AUTH_CONFIG = $taskAuthJson
} finally {
    [Environment]::SetEnvironmentVariable('WEIPAI_AUTH_SETUP_PASSWORD', $null, 'Process')
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskPasswordPointer)
    $taskPassword.Dispose()
}
Write-Host '已设置当前进程 AUTH_CONFIG；启动 API 时继承，不显示凭证、不写文件。'

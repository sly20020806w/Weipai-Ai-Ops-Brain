function Get-ProjectUv {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $env:PYTHONIOENCODING = 'utf-8'
    if (-not $env:UV_CACHE_DIR) {
        $env:UV_CACHE_DIR = Join-Path $projectRoot '.cache\uv'
    }
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCommand) {
        return $uvCommand.Source
    }
    $localUv = Join-Path $projectRoot '.tools\uv\bin\uv.exe'
    if (Test-Path -LiteralPath $localUv) {
        return $localUv
    }
    throw '未找到 uv。请先安装 uv 并将其加入 PATH，然后重新执行。'
}

function Get-ProjectPnpm {
    $pnpmCommand = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
    if (-not $pnpmCommand) { $pnpmCommand = Get-Command pnpm -ErrorAction SilentlyContinue }
    if (-not $pnpmCommand) { throw '未找到 pnpm 11，请先安装并将其加入 PATH。' }
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCommand) { throw '未找到 Node.js；本项目要求 22.18+，推荐 24 LTS。' }
    return $pnpmCommand.Source
}
